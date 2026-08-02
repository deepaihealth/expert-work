/**
 * TriggersList tests — Stream H.4 PR 6.
 *
 * Backend returns raw payloads (per audit.py / curation.py / triggers.py
 * pattern), so adapter mocks deliver ``{items, total, cross_tenant}``
 * objects without the envelope.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { TriggersList } from "../TriggersList";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

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

function renderTriggers() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <TriggersList />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const cronRow = {
  id: "t1",
  tenant_id: "t1",
  user_id: null,
  agent_name: "reporter",
  agent_version: "1.0",
  name: "daily_summary",
  kind: "cron" as const,
  config: { expr: "0 9 * * *" },
  enabled: true,
  source: "api",
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

const webhookRow = {
  ...cronRow,
  id: "t2",
  name: "external_event",
  kind: "webhook" as const,
  config: {},
};

beforeEach(() => {
  vi.restoreAllMocks();
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("TriggersList", () => {
  it("home 态启停/创建可用;切入态置灰(两态)", async () => {
    const routes: RouteHandler[] = [
      {
        match: (u) => u === "/v1/triggers",
        respond: () => ({ items: [cronRow], total: 1, cross_tenant: false }),
      },
    ];

    installAdapter(routes);
    const first = renderTriggers();
    await waitFor(() =>
      expect(screen.getByTestId("trigger-enabled-t1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("trigger-enabled-t1")).toBeEnabled();
    expect(screen.getByTestId("triggers-create-btn")).toBeEnabled();
    expect(screen.getByTestId("trigger-delete-t1")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    installAdapter(routes);
    renderTriggers();
    await waitFor(() =>
      expect(screen.getByTestId("trigger-enabled-t1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("trigger-enabled-t1")).toBeDisabled();
    expect(screen.getByTestId("triggers-create-btn")).toBeDisabled();
    expect(screen.getByTestId("trigger-delete-t1")).toBeDisabled();
  });

  it("default tab is cron and lists cron triggers only", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/triggers",
        respond: () => ({
          items: [cronRow, webhookRow],
          total: 2,
          cross_tenant: false,
        }),
      },
    ]);
    renderTriggers();
    await waitFor(() => expect(screen.getByText("daily_summary")).toBeInTheDocument());
    // Webhook row should NOT appear on the default (cron) tab.
    expect(screen.queryByText("external_event")).toBeNull();
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
  });

  it("switching to Webhook tab filters to webhook triggers", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/triggers",
        respond: () => ({
          items: [cronRow, webhookRow],
          total: 2,
          cross_tenant: false,
        }),
      },
    ]);
    const user = userEvent.setup();
    renderTriggers();
    await waitFor(() => expect(screen.getByText("daily_summary")).toBeInTheDocument());
    await user.click(screen.getByText(/Webhook \(/));
    await waitFor(() => expect(screen.getByText("external_event")).toBeInTheDocument());
    expect(screen.queryByText("daily_summary")).toBeNull();
    // Webhook column shows the path.
    expect(screen.getByText(/POST \/v1\/webhooks\/t2/)).toBeInTheDocument();
  });

  it("create cron drawer has cron_expr field; submit creates trigger", async () => {
    let postBody: unknown = null;
    installAdapter([
      {
        match: (u, m) => u === "/v1/triggers" && m === "get",
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/triggers" && m === "post",
        respond: ({ data }) => {
          postBody = JSON.parse(data as string);
          return { ...cronRow, name: "new_cron" };
        },
        status: 201,
      },
    ]);
    const user = userEvent.setup();
    renderTriggers();
    await waitFor(() => expect(screen.getByTestId("triggers-create-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("triggers-create-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("trigger-cron-expr-input")).toBeInTheDocument(),
    );
    await user.type(screen.getByTestId("trigger-name-input"), "new_cron");
    await user.type(screen.getByTestId("trigger-agent-name-input"), "reporter");
    await user.type(screen.getByTestId("trigger-agent-version-input"), "1.0");
    await user.type(screen.getByTestId("trigger-cron-expr-input"), "0 12 * * *");
    await user.click(screen.getByRole("button", { name: /^Create$/ }));
    await waitFor(() => {
      const body = postBody as Record<string, unknown>;
      expect(body.kind).toBe("cron");
      expect(body.config).toEqual({ expr: "0 12 * * *" });
    });
  });

  it("create webhook surfaces show-once secret drawer", async () => {
    installAdapter([
      {
        match: (u, m) => u === "/v1/triggers" && m === "get",
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/triggers" && m === "post",
        respond: () => ({
          ...webhookRow,
          name: "my_hook",
          webhook_secret: "ew_whk_abc123def456",
        }),
        status: 201,
      },
    ]);
    const user = userEvent.setup();
    renderTriggers();
    await waitFor(() => expect(screen.getByTestId("triggers-create-btn")).toBeInTheDocument());
    // Switch to Webhook tab so the Create button opens the webhook form.
    await user.click(screen.getByText(/Webhook \(/));
    await user.click(screen.getByTestId("triggers-create-btn"));
    await user.type(screen.getByTestId("trigger-name-input"), "my_hook");
    await user.type(screen.getByTestId("trigger-agent-name-input"), "reporter");
    await user.type(screen.getByTestId("trigger-agent-version-input"), "1.0");
    await user.click(screen.getByRole("button", { name: /^Create$/ }));
    await waitFor(() =>
      expect(screen.getByTestId("trigger-secret-value")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("trigger-secret-value").textContent).toBe(
      "ew_whk_abc123def456",
    );
  });

  it("enabled toggle PATCHes the trigger", async () => {
    let patchBody: unknown = null;
    installAdapter([
      {
        match: (u, m) => u === "/v1/triggers" && m === "get",
        respond: () => ({ items: [cronRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/triggers/t1" && m === "patch",
        respond: ({ data }) => {
          patchBody = JSON.parse(data as string);
          return { ...cronRow, enabled: false };
        },
      },
    ]);
    const user = userEvent.setup();
    renderTriggers();
    await waitFor(() => expect(screen.getByTestId("trigger-enabled-t1")).toBeInTheDocument());
    // Antd Switch renders as a button — click toggles it.
    await user.click(screen.getByTestId("trigger-enabled-t1"));
    await waitFor(() => expect((patchBody as Record<string, unknown>).enabled).toBe(false));
  });
});
