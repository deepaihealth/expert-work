/**
 * EvalRunsList tests — P1-S2.5-FE.
 *
 * The eval-runs SDK is stubbed; the page renders inside a MemoryRouter +
 * antd ``App`` (the page uses ``App.useApp()`` for toasts). Rows use
 * terminal statuses so the live-poll timer stays off during the test.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { setStoredToken } from "../../api/client";
import "../../i18n";

import * as evalSdk from "../../api/eval_runs";
import { EvalRunsList } from "../EvalRunsList";
import type { EvalRunRecord } from "../../api/eval_runs";

// Cross-tenant W3 — override the hook only (the real TenantScopeProvider in
// renderPage keeps working via the importOriginal spread); switchable per
// test, undefined = home state.
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

function run(overrides: Partial<EvalRunRecord> = {}): EvalRunRecord {
  return {
    id: crypto.randomUUID(),
    suite: "m0_baseline",
    status: "passed",
    triggered_by: "manual",
    summary: { pass_count: 15, total: 15 },
    created_at: "2026-06-14T08:00:00Z",
    started_at: "2026-06-14T08:00:05Z",
    finished_at: "2026-06-14T08:02:30Z",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/eval-runs"]}>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <EvalRunsList />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname + loc.search}</div>;
}

// W4 — routed render so the aggregate row-jump's target URL is observable.
function renderRouted() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["system_admin"] }));
  return render(
    <MemoryRouter initialEntries={["/eval-runs"]}>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <Routes>
              <Route path="/eval-runs" element={<EvalRunsList />} />
              <Route path="/eval-runs/:runId" element={<LocationProbe />} />
            </Routes>
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  scopeRef.current = undefined;
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("EvalRunsList", () => {
  it("renders runs from the SDK", async () => {
    const row = run();
    vi.spyOn(evalSdk, "listEvalRuns").mockResolvedValue({ items: [row], total: 1 });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    expect(screen.getByText("15/15")).toBeInTheDocument();
    expect(screen.getByText("passed")).toBeInTheDocument();
  });

  it("enqueue button posts a baseline run and refreshes", async () => {
    const listMock = vi
      .spyOn(evalSdk, "listEvalRuns")
      .mockResolvedValue({ items: [], total: 0 });
    const enqueueMock = vi
      .spyOn(evalSdk, "enqueueEvalRun")
      .mockResolvedValue(run({ status: "queued", summary: null }));

    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("eval-enqueue"));

    await waitFor(() => expect(enqueueMock).toHaveBeenCalledWith("m0_baseline"));
    // initial load + post-enqueue refresh
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it("home 态入队可用;切入态置灰(两态)", async () => {
    vi.spyOn(evalSdk, "listEvalRuns").mockResolvedValue({ items: [], total: 0 });

    const first = renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    expect(screen.getByTestId("eval-enqueue")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    expect(screen.getByTestId("eval-enqueue")).toBeDisabled();
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(evalSdk, "listEvalRuns").mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-error")).toBeInTheDocument());
  });

  it("threads the ambient tenant scope into listEvalRuns (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    const listMock = vi
      .spyOn(evalSdk, "listEvalRuns")
      .mockResolvedValue({ items: [], total: 0 });

    renderPage();

    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ tenantScope: scopeRef.current }),
      ),
    );
  });

  // ─── Cross-tenant W4 — "*" aggregate: tenant column + row-jump ───────

  it("aggregate shows the tenant column and the row-jump carries tenant_id (W4)", async () => {
    scopeRef.current = "*";
    const row = run({ tenant_id: "tenant-2-xxxx" });
    vi.spyOn(evalSdk, "listEvalRuns").mockResolvedValue({ items: [row], total: 1 });

    renderRouted();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    // Tenant column renders the truncated owning tenant.
    expect(screen.getByText("tenant-2…")).toBeInTheDocument();

    // JWT home tenant is t1; the row belongs to tenant-2 → the click
    // appends ?tenant_id= so the detail reads hit the owning tenant.
    // (scope to the table — the suite Select also renders "m0_baseline")
    await userEvent.click(within(screen.getByTestId("eval-table")).getByText("m0_baseline"));
    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent(
        `/eval-runs/${row.id}?tenant_id=tenant-2-xxxx`,
      ),
    );
  });

  it("aggregate greys out enqueue (the run would land in the caller's own tenant)", async () => {
    scopeRef.current = "*";
    vi.spyOn(evalSdk, "listEvalRuns").mockResolvedValue({ items: [], total: 0 });

    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    expect(screen.getByTestId("eval-enqueue")).toBeDisabled();
  });

  it("home scope hides the tenant column (W4)", async () => {
    const row = run({ tenant_id: "t1" });
    vi.spyOn(evalSdk, "listEvalRuns").mockResolvedValue({ items: [row], total: 1 });

    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-table")).toBeInTheDocument());
    expect(screen.queryByText("t1…")).not.toBeInTheDocument();
  });
});
