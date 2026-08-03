/**
 * Quality dashboard tests — Stream RT-5 (RT-ADR-26).
 *
 * The ``/v1/quality`` SDK returns the raw ``{ items }`` payload (no envelope),
 * so the adapter returns that directly for the quality URLs and the
 * ``{success,data}`` envelope only for the ``/me`` bootstrap.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import "../../i18n";

import { SettingsQuality } from "../SettingsQuality";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

// Cross-tenant W3 — the page reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (switchable per test;
// undefined = home state).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const TENANT = "00000000-0000-0000-0000-00000000acme";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

const SCORES = [
  {
    id: 1,
    agent_name: "support-bot",
    agent_version: "1",
    run_id: "run-abc",
    thread_id: "thread-xyz",
    overall: 2,
    dimensions: { addressed_request: 2, coherence: 3, safety: 5 },
    rationale: "missed the question",
    judge_model: "claude-haiku-4-5-20251001",
    observed_at: "2026-07-06T11:50:00Z",
  },
  {
    id: 2,
    agent_name: "support-bot",
    agent_version: "1",
    run_id: "run-def",
    thread_id: "thread-uvw",
    overall: 5,
    dimensions: { addressed_request: 5, coherence: 5, safety: 5 },
    rationale: "great",
    judge_model: "claude-haiku-4-5-20251001",
    observed_at: "2026-07-06T09:00:00Z",
  },
];

const ALERTS = [
  {
    id: 1,
    agent_name: "support-bot",
    recent_mean: 2.7,
    baseline_mean: 4.6,
    drift_pct: 0.413,
    recent_count: 18,
    baseline_count: 92,
    detected_at: "2026-07-06T09:15:00Z",
  },
];

function installAdapter(scores: unknown[], alerts: unknown[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    let data: unknown = {};
    if (url.endsWith("/me")) {
      data = {
        success: true,
        data: {
          subject_id: "u1",
          subject_type: "user",
          tenant_id: TENANT,
          auth_method: "jwt",
          roles: ["member"],
          scopes: [],
          is_system_admin: false,
          allowed_tenants: [TENANT],
        },
        error: null,
      };
    } else if (url.endsWith("/quality/scores")) {
      data = { items: scores };
    } else if (url.endsWith("/quality/drift-alerts")) {
      data = { items: alerts };
    }
    return Promise.resolve({
      data,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderPage() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["member"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsQuality />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  scopeRef.current = undefined;
});

describe("SettingsQuality page", () => {
  it("renders drift, per-agent trend, and low-score drill sections", async () => {
    installAdapter(SCORES, ALERTS);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-drift-table")).toBeInTheDocument(),
    );
    // Drift alert row shows the drop and means.
    const driftTable = screen.getByTestId("quality-drift-table");
    expect(within(driftTable).getByText("-41.3%")).toBeInTheDocument();
    // Per-agent trend + low-score tables present.
    expect(screen.getByTestId("quality-trend-table")).toBeInTheDocument();
    expect(screen.getByTestId("quality-low-table")).toBeInTheDocument();
  });

  it("low-score row links to the run_detail of the sampled run", async () => {
    installAdapter(SCORES, ALERTS);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-low-table")).toBeInTheDocument(),
    );
    const lowTable = screen.getByTestId("quality-low-table");
    // The worst run (run-abc / thread-xyz) links to run_detail.
    const link = within(lowTable)
      .getAllByRole("link")
      .find((a) => a.getAttribute("href") === "/runs/thread-xyz/run-abc");
    expect(link).toBeDefined();
  });

  it("shows empty states when there is no data", async () => {
    installAdapter([], []);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-drift-empty")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("quality-trend-empty")).toBeInTheDocument();
    expect(screen.getByTestId("quality-low-empty")).toBeInTheDocument();
  });

  it("surfaces an error alert when a fetch fails", async () => {
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      if (url.endsWith("/me")) {
        return Promise.resolve({
          data: {
            success: true,
            data: {
              subject_id: "u1",
              subject_type: "user",
              tenant_id: TENANT,
              auth_method: "jwt",
              roles: ["member"],
              scopes: [],
              is_system_admin: false,
              allowed_tenants: [TENANT],
            },
            error: null,
          },
          status: 200,
          statusText: "OK",
          headers: {},
          config,
          request: {},
        });
      }
      return Promise.reject(new Error("boom"));
    };
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-error")).toBeInTheDocument(),
    );
  });

  it("threads the switched tenant scope onto both quality reads (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    const seen: Record<string, Record<string, unknown> | undefined> = {};
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      let data: unknown = {};
      if (url.endsWith("/quality/scores")) {
        seen.scores = config.params as Record<string, unknown>;
        data = { items: [] };
      } else if (url.endsWith("/quality/drift-alerts")) {
        seen.alerts = config.params as Record<string, unknown>;
        data = { items: [] };
      }
      return Promise.resolve({
        data,
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    renderPage();
    await waitFor(() => expect(seen.scores?.tenant_id).toBe(scopeRef.current));
    expect(seen.alerts?.tenant_id).toBe(scopeRef.current);
  });

  // ─── Cross-tenant W4 — "*" aggregate: tenant column + per-tenant split ──

  it("aggregate splits same-named agents per tenant and shows the tenant column (W4)", async () => {
    scopeRef.current = "*";
    installAdapter(
      [
        { ...SCORES[0], tenant_id: "tenant-2-xxxx" },
        { ...SCORES[1], tenant_id: "tenant-3-yyyy" },
      ],
      [{ ...ALERTS[0], tenant_id: "tenant-2-xxxx" }],
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-trend-table")).toBeInTheDocument(),
    );
    // Same-named agent in two tenants → two trend rows, not one folded mean.
    const trendTable = screen.getByTestId("quality-trend-table");
    expect(within(trendTable).getAllByText("support-bot")).toHaveLength(2);
    // Tenant column renders the truncated owning tenant on every table.
    expect(within(trendTable).getByText("tenant-2…")).toBeInTheDocument();
    expect(within(trendTable).getByText("tenant-3…")).toBeInTheDocument();
    const driftTable = screen.getByTestId("quality-drift-table");
    expect(within(driftTable).getByText("tenant-2…")).toBeInTheDocument();
    const lowTable = screen.getByTestId("quality-low-table");
    expect(within(lowTable).getByText("tenant-2…")).toBeInTheDocument();
  });

  it("home scope keeps a single trend row per agent and no tenant column (W4)", async () => {
    installAdapter(SCORES, ALERTS);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("quality-trend-table")).toBeInTheDocument(),
    );
    // Both scores share one agent + tenant → one trend row (pre-W4 shape).
    const trendTable = screen.getByTestId("quality-trend-table");
    expect(within(trendTable).getAllByText("support-bot")).toHaveLength(1);
    expect(screen.queryByText(/^tenant-/)).not.toBeInTheDocument();
  });
});
