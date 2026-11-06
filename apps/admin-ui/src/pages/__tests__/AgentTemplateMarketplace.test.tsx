/**
 * Agent Template Marketplace tests — Stream Agent-Templates (M1-6b).
 *
 * Covers the card-wall render (display_name + name@version + i18n category),
 * a tier-locked card (can_fork=false → Fork button absent, lock present), and
 * the fork flow (open modal → submit → POST /v1/agents/fork). Mirrors
 * SettingsAgentTemplates.test.tsx.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../../i18n";

import { AgentTemplateMarketplace } from "../AgentTemplateMarketplace";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

const TENANT = "00000000-0000-0000-0000-00000000acme";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (method: string, url: string) => boolean;
  respond: (body: unknown) => unknown;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const handler = handlers.find((h) => h.match(method, url));
    const parsed = config.data ? JSON.parse(config.data as string) : undefined;
    return Promise.resolve({
      data: handler?.respond(parsed) ?? {},
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

const FREE_TEMPLATE = {
  name: "support-bot",
  version: "1.0.0",
  display_name: "Support Bot",
  description: "Customer support",
  category: "support",
  icon: null,
  required_tier: "free" as const,
  can_fork: true,
};

const LOCKED_TEMPLATE = {
  name: "sales-pro",
  version: "2.0.0",
  display_name: "Sales Pro",
  description: "Enterprise sales",
  category: "sales",
  icon: null,
  required_tier: "enterprise" as const,
  can_fork: false,
};

function renderPage() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["operator"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <AgentTemplateMarketplace />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("AgentTemplateMarketplace page", () => {
  it("renders cards with display_name, ref, and i18n category", async () => {
    installAdapter([
      {
        match: (m, u) => m === "get" && u.endsWith("/agents/templates"),
        respond: () => ({ success: true, data: [FREE_TEMPLATE, LOCKED_TEMPLATE], error: null }),
      },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("atm-root")).toBeInTheDocument());
    expect(screen.getByText("Support Bot")).toBeInTheDocument();
    expect(screen.getByText("support-bot@1.0.0")).toBeInTheDocument();
    // Category renders the i18n label, not the raw slug.
    expect(screen.getByText("Support")).toBeInTheDocument();
    // Forkable card → fork button; locked card → lock, no fork button.
    expect(screen.getByTestId("atm-fork-support-bot")).toBeInTheDocument();
    expect(screen.getByTestId("atm-locked-sales-pro")).toBeInTheDocument();
    expect(screen.queryByTestId("atm-fork-sales-pro")).not.toBeInTheDocument();
  });

  it("forks a template via the modal", async () => {
    let forkBody: unknown = null;
    installAdapter([
      {
        match: (m, u) => m === "post" && u.endsWith("/agents/fork"),
        respond: (body) => {
          forkBody = body;
          return {
            success: true,
            data: { record: { name: "my-bot", version: "1.0.0" } },
            error: null,
          };
        },
      },
      {
        match: (m, u) => m === "get" && u.endsWith("/agents/templates"),
        respond: () => ({ success: true, data: [FREE_TEMPLATE], error: null }),
      },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("atm-fork-support-bot")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("atm-fork-support-bot"));

    const input = await screen.findByTestId("atm-fork-name");
    fireEvent.change(input, { target: { value: "my-bot" } });
    // The modal's OK button carries the "Fork" label.
    fireEvent.click(screen.getAllByText("Fork").slice(-1)[0]);

    await waitFor(() =>
      expect(forkBody).toEqual({
        template_name: "support-bot",
        template_version: "1.0.0",
        name: "my-bot",
      }),
    );
  });

  // B-278 — forking onto a name held by a deleted agent: the real 409 envelope
  // goes through the axios interceptor, and the modal explains restore / purge
  // with a link to that agent instead of a raw error toast.
  it("explains a clash with a deleted agent and links to it", async () => {
    apiClient.defaults.adapter = (config) => {
      if ((config.method ?? "").toLowerCase() === "post") {
        const error = Object.assign(new Error("Request failed with status code 409"), {
          isAxiosError: true,
          config,
          response: {
            status: 409,
            data: {
              success: false,
              data: null,
              error: {
                code: "MANIFEST_DUPLICATE_DELETED",
                message: "a deleted agent with this name and version exists",
                details: { name: "my-bot", version: "1.0.0", status: "deleted" },
              },
            },
          },
        });
        return Promise.reject(error);
      }
      return Promise.resolve({
        data: (config.url ?? "").endsWith("/agents/templates")
          ? { success: true, data: [FREE_TEMPLATE], error: null }
          : {},
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    renderPage();
    await waitFor(() => expect(screen.getByTestId("atm-fork-support-bot")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("atm-fork-support-bot"));
    fireEvent.change(await screen.findByTestId("atm-fork-name"), { target: { value: "my-bot" } });
    fireEvent.click(screen.getAllByText("Fork").slice(-1)[0]);

    const notice = await screen.findByTestId("duplicate-deleted-notice");
    expect(notice).toHaveTextContent("A deleted agent with the same name exists");
    expect(screen.getByTestId("duplicate-deleted-link")).toHaveTextContent("Open my-bot v1.0.0");
  });
});
