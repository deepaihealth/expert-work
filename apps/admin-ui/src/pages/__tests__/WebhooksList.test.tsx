/**
 * WebhooksList tests — HX-9 (STREAM-HX § 13).
 *
 * Backend returns raw ``{items,total,cross_tenant}`` payloads (no
 * envelope), mirroring triggers — adapter mocks deliver them directly.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { WebhooksList } from "../WebhooksList";
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

function renderWebhooks() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <WebhooksList />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const endpointRow = {
  id: "w1",
  name: "ops-notify",
  url: "https://hooks.example.com/ingest",
  event_types: ["run.completed", "run.failed"] as const,
  agent_name: null,
  enabled: true,
  source: "api",
  created_at: "2026-06-13T10:00:00Z",
  updated_at: "2026-06-13T10:00:00Z",
};

beforeEach(() => {
  vi.restoreAllMocks();
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("WebhooksList", () => {
  it("home 态启停/创建可用;切入态置灰(两态)", async () => {
    const routes: RouteHandler[] = [
      {
        match: (u) => u === "/v1/webhook-endpoints",
        respond: () => ({ items: [endpointRow], total: 1, cross_tenant: false }),
      },
    ];

    installAdapter(routes);
    const first = renderWebhooks();
    await waitFor(() =>
      expect(screen.getByTestId("webhook-enabled-w1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("webhook-enabled-w1")).toBeEnabled();
    expect(screen.getByTestId("webhooks-create-btn")).toBeEnabled();
    expect(screen.getByTestId("webhook-delete-w1")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    installAdapter(routes);
    renderWebhooks();
    await waitFor(() =>
      expect(screen.getByTestId("webhook-enabled-w1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("webhook-enabled-w1")).toBeDisabled();
    expect(screen.getByTestId("webhooks-create-btn")).toBeDisabled();
    expect(screen.getByTestId("webhook-delete-w1")).toBeDisabled();
  });

  it("lists endpoints with their event tags", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/webhook-endpoints",
        respond: () => ({ items: [endpointRow], total: 1, cross_tenant: false }),
      },
    ]);
    renderWebhooks();
    await waitFor(() => expect(screen.getByText("ops-notify")).toBeInTheDocument());
    expect(screen.getByText("run.completed")).toBeInTheDocument();
    expect(screen.getByText("run.failed")).toBeInTheDocument();
    // agent_name null renders the "all agents" placeholder.
    expect(screen.getByText("All agents")).toBeInTheDocument();
  });

  it("shows cross-tenant banner when backend says so", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/webhook-endpoints",
        respond: () => ({ items: [endpointRow], total: 1, cross_tenant: true }),
      },
    ]);
    renderWebhooks();
    await waitFor(() =>
      expect(screen.getByTestId("webhooks-cross-banner")).toBeInTheDocument(),
    );
  });

  it("create surfaces the show-once signing secret drawer", async () => {
    installAdapter([
      {
        match: (u, m) => u === "/v1/webhook-endpoints" && m === "get",
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/webhook-endpoints" && m === "post",
        respond: () => ({ ...endpointRow, name: "my-hook", secret: "ew_whsec_abc123" }),
        status: 201,
      },
    ]);
    const user = userEvent.setup();
    renderWebhooks();
    await waitFor(() => expect(screen.getByTestId("webhooks-create-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("webhooks-create-btn"));
    await user.type(screen.getByTestId("webhook-name-input"), "my-hook");
    await user.type(screen.getByTestId("webhook-url-input"), "https://h.example.com/x");

    // Open the antd multi-select (mouseDown on the combobox opens the
    // portal dropdown) and pick one event type by its option title.
    const combobox = within(screen.getByTestId("webhook-events-select")).getByRole("combobox");
    fireEvent.mouseDown(combobox);
    await user.click(await screen.findByTitle("run.completed"));

    await user.click(screen.getByRole("button", { name: /^Create$/ }));
    await waitFor(() =>
      expect(screen.getByTestId("webhook-secret-value")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("webhook-secret-value").textContent).toBe("ew_whsec_abc123");
  });

  it("enabled toggle PATCHes the endpoint", async () => {
    let patchBody: unknown = null;
    installAdapter([
      {
        match: (u, m) => u === "/v1/webhook-endpoints" && m === "get",
        respond: () => ({ items: [endpointRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/webhook-endpoints/w1" && m === "patch",
        respond: ({ data }) => {
          patchBody = JSON.parse(data as string);
          return { ...endpointRow, enabled: false };
        },
      },
    ]);
    const user = userEvent.setup();
    renderWebhooks();
    await waitFor(() => expect(screen.getByTestId("webhook-enabled-w1")).toBeInTheDocument());
    await user.click(screen.getByTestId("webhook-enabled-w1"));
    await waitFor(() => expect((patchBody as Record<string, unknown>).enabled).toBe(false));
  });
});
