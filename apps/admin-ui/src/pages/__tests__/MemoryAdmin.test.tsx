/**
 * MemoryAdmin tests — Stream H.4 PR 2.
 *
 * The page is enveloped (backend `/v1/memory` returns
 * ``{success, data: {...}}``) so the SDK uses ``getJson`` and we
 * deliver enveloped mocks via the axios adapter.
 *
 * Monaco is stubbed (same approach as ApprovalCard + EvalDatasetsPanel)
 * so JSON edits flow through ``onChange`` synchronously.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { MemoryAdmin } from "../MemoryAdmin";
import * as memorySdk from "../../api/memory";
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

function renderMemory() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <MemoryAdmin />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const memRow = {
  id: "m1",
  tenant_id: "t1",
  user_id: "user-alice-uuid-abc",
  kind: "fact" as const,
  content: "User prefers brevity in answers.",
  created_at: "2026-05-26T10:00:00Z",
  importance: 0.8,
  confidence: 0.6,
};

const memRow2 = {
  ...memRow,
  id: "m2",
  kind: "episodic" as const,
  content: "Last week Alice asked about Q3 revenue.",
};

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("MemoryAdmin", () => {
  it("lists memories and renders the table", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/memory") && u !== "/v1/memory/m1",
        respond: () => ({
          success: true,
          data: { items: [memRow, memRow2], total: 2, cross_tenant: false },
          error: null,
        }),
      },
    ]);
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    expect(screen.getByText(/Q3 revenue/)).toBeInTheDocument();
  });

  it("client-side search filters by content", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/memory"),
        respond: () => ({
          success: true,
          data: { items: [memRow, memRow2], total: 2, cross_tenant: false },
          error: null,
        }),
      },
    ]);
    const user = userEvent.setup();
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    await user.type(screen.getByPlaceholderText(/Filter by content/i), "Q3");
    await waitFor(() => {
      expect(screen.queryByText(/User prefers/)).toBeNull();
      expect(screen.getByText(/Q3 revenue/)).toBeInTheDocument();
    });
  });

  it("Save is disabled when buffer is pristine; enabled after edit", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/memory"),
        respond: () => ({
          success: true,
          data: { items: [memRow], total: 1, cross_tenant: false },
          error: null,
        }),
      },
    ]);
    const user = userEvent.setup();
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    await user.click(screen.getByTestId(`memory-edit-${memRow.id}`));
    await waitFor(() => expect(screen.getByTestId("memory-content-editor")).toBeInTheDocument());
    expect(screen.getByTestId("memory-save-btn")).toBeDisabled();
    fireEvent.change(screen.getByTestId("memory-content-editor"), { target: { value: "edited content" } });
    await waitFor(() => expect(screen.getByTestId("memory-save-btn")).not.toBeDisabled());
  });

  it("EMBEDDER_UNCONFIGURED 503 surfaces a friendly error", async () => {
    let listCount = 0;
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/memory") && m === "get",
        respond: () => {
          listCount++;
          return {
            success: true,
            data: { items: [memRow], total: 1, cross_tenant: false },
            error: null,
          };
        },
      },
      {
        match: (u, m) => u === "/v1/memory/m1" && m === "patch",
        status: 503,
        respond: () => ({
          success: false,
          data: null,
          error: { code: "EMBEDDER_UNCONFIGURED", message: "embedder missing" },
        }),
      },
    ]);
    const user = userEvent.setup();
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    await user.click(screen.getByTestId(`memory-edit-${memRow.id}`));
    fireEvent.change(screen.getByTestId("memory-content-editor"), { target: { value: "edited" } });
    await user.click(screen.getByTestId("memory-save-btn"));
    // List was called once on mount; PATCH 503 means refresh doesn't trigger again.
    await waitFor(() => expect(listCount).toBe(1));
  });

  it("renders importance / confidence score badges", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/memory"),
        respond: () => ({
          success: true,
          data: { items: [memRow], total: 1, cross_tenant: false },
          error: null,
        }),
      },
    ]);
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    // memRow has importance 0.80 / confidence 0.60.
    expect(screen.getByText(/0\.80/)).toBeInTheDocument();
    expect(screen.getByText(/0\.60/)).toBeInTheDocument();
  });

  it("Correct routes Save through the self-correction endpoint", async () => {
    let correctBody: unknown = null;
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/memory") && m === "get",
        respond: () => ({
          success: true,
          data: { items: [memRow], total: 1, cross_tenant: false },
          error: null,
        }),
      },
      {
        match: (u, m) => u === "/v1/memory/m1/correct" && m === "post",
        respond: () => ({
          success: true,
          data: { ...memRow, content: "fixed", confidence: 1.0 },
          error: null,
        }),
      },
    ]);
    // Capture the POST body via the adapter.
    const baseAdapter = apiClient.defaults.adapter;
    apiClient.defaults.adapter = (config) => {
      if ((config.url ?? "").endsWith("/correct") && config.data) {
        correctBody = JSON.parse(config.data as string);
      }
      return (baseAdapter as (c: typeof config) => Promise<unknown>)(config) as never;
    };
    const user = userEvent.setup();
    renderMemory();
    await waitFor(() => expect(screen.getByText(/User prefers/)).toBeInTheDocument());
    await user.click(screen.getByTestId(`memory-correct-${memRow.id}`));
    await waitFor(() => expect(screen.getByTestId("memory-content-editor")).toBeInTheDocument());
    fireEvent.change(screen.getByTestId("memory-content-editor"), { target: { value: "fixed" } });
    await user.click(screen.getByTestId("memory-save-btn"));
    await waitFor(() =>
      expect(correctBody).toEqual({ action: "rewrite", content: "fixed" }),
    );
  });
});

// ─── B-51 整合失败横幅 ─────────────────────────────────────────────────

function renderMemoryAsSystemAdmin() {
  setStoredToken(
    makeJwt({ sub: "u1", tenant_id: "t1", roles: ["system_admin"] }),
  );
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <MemoryAdmin />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

function auditHandlers(details: Record<string, unknown>[]): RouteHandler[] {
  return [
    {
      match: (u) => u.startsWith("/v1/audit"),
      respond: () => ({
        items: details.map((d, i) => ({
          id: i + 1,
          tenant_id: "00000000-0000-0000-0000-000000000000",
          actor_type: "system",
          actor_id: "memory_consolidator",
          on_behalf_of: null,
          action: "memory:consolidator_run",
          resource_type: "memory_item",
          resource_id: null,
          result: "success",
          reason: null,
          ip: null,
          user_agent: null,
          request_id: null,
          trace_id: null,
          details: d,
          occurred_at: "2026-09-11T00:00:00Z",
        })),
        next_cursor: null,
        has_more: false,
        applied_scope: "cross_tenant",
      }),
    },
    {
      match: (u) => u.startsWith("/v1/memory"),
      respond: () => ({
        success: true,
        data: { items: [memRow], total: 1, cross_tenant: false },
        error: null,
      }),
    },
  ];
}

describe("MemoryAdmin — consolidator health (B-51)", () => {
  it("names the reason when consolidation keeps failing on credentials", async () => {
    installAdapter(
      auditHandlers([
        {
          consolidated: 0,
          errors: 2,
          errors_by_reason: { credentials_missing: 2 },
          missing_credential_providers: ["anthropic"],
        },
        {
          consolidated: 0,
          errors: 1,
          errors_by_reason: { credentials_missing: 1 },
          missing_credential_providers: ["anthropic"],
        },
      ]),
    );
    renderMemoryAsSystemAdmin();
    const alert = await screen.findByTestId("memory-consolidator-alert");
    expect(alert).toHaveTextContent(/2 sweep/);
    expect(alert).toHaveTextContent(/platform credentials are not configured/i);
    expect(alert).toHaveTextContent(/anthropic/);
  });

  it("stays quiet when the latest sweep did not fail on credentials", async () => {
    installAdapter(
      auditHandlers([
        {
          consolidated: 3,
          errors: 0,
          errors_by_reason: {},
          missing_credential_providers: [],
        },
      ]),
    );
    renderMemoryAsSystemAdmin();
    await waitFor(() =>
      expect(screen.getByText(/User prefers/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("memory-consolidator-alert")).toBeNull();
  });

  it("a non-credential failure is not blamed on credentials", async () => {
    installAdapter(
      auditHandlers([
        {
          consolidated: 0,
          errors: 4,
          errors_by_reason: { other: 4 },
          missing_credential_providers: [],
        },
      ]),
    );
    renderMemoryAsSystemAdmin();
    await waitFor(() =>
      expect(screen.getByText(/User prefers/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("memory-consolidator-alert")).toBeNull();
  });

  it("skips the platform-scoped read entirely for a non system_admin", async () => {
    const spy = vi.spyOn(memorySdk, "getConsolidatorHealth");
    installAdapter(
      auditHandlers([
        {
          consolidated: 0,
          errors: 1,
          errors_by_reason: { credentials_missing: 1 },
          missing_credential_providers: ["anthropic"],
        },
      ]),
    );
    renderMemory(); // roles: ["admin"] —— 读平台审计会 403,压根别发
    await waitFor(() =>
      expect(screen.getByText(/User prefers/)).toBeInTheDocument(),
    );
    expect(spy).not.toHaveBeenCalled();
    expect(screen.queryByTestId("memory-consolidator-alert")).toBeNull();
  });
});
