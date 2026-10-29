/**
 * KnowledgeAdmin (list page) tests — KB commercial uplift.
 *
 * The knowledge SDK is stubbed. Covers: bases render with stats +
 * needs-reindex tag, row navigation to the detail page, the create modal
 * (createBase + 409 duplicate), and the H-19 scope note.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import { KnowledgeAdmin } from "../KnowledgeAdmin";
import { AuthProvider } from "../../auth/AuthContext";
import { setStoredToken } from "../../api/client";

// Cross-tenant W3 (F3) — 共享 tenant scope mock 工厂;scopeRef.current
// 切换切入/聚合视角,undefined = home 态。
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

const BASES: knowledgeSdk.KnowledgeBase[] = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    name: "support-docs",
    chunk_max_tokens: 512,
    chunk_overlap_tokens: 64,
    created_at: "2026-06-12T00:00:00Z",
    description: "Customer FAQ",
    needs_reindex: true,
    stats: { document_count: 3, chunk_count: 42 },
  },
];

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname + loc.search}</div>;
}

/** ``listBasesPage`` answering with one page holding ``bases``. */
function mockBases(bases: knowledgeSdk.KnowledgeBase[]) {
  return vi
    .spyOn(knowledgeSdk, "listBasesPage")
    .mockResolvedValue({ bases, total: bases.length, offset: 0, limit: 20 });
}

function knowledgeTree() {
  return (
    <MemoryRouter initialEntries={["/knowledge"]}>
      <AuthProvider>
        <App>
          <Routes>
            <Route path="/knowledge" element={<KnowledgeAdmin />} />
            <Route path="/knowledge/:name" element={<LocationProbe />} />
          </Routes>
        </App>
      </AuthProvider>
    </MemoryRouter>
  );
}

function renderPage() {
  // W4 — the page reads useAuth() (home tenant for the aggregate row-jump).
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(knowledgeTree());
}

beforeEach(() => {
  scopeRef.current = undefined;
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

afterEach(() => vi.restoreAllMocks());

describe("KnowledgeAdmin (list)", () => {
  it("renders bases with stats + needs-reindex tag", async () => {
    mockBases(BASES);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByText("Customer FAQ")).toBeInTheDocument();
    expect(screen.getByText("Needs re-index")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument(); // chunk count
  });

  it("navigates to the detail page on row click", async () => {
    mockBases(BASES);

    renderPage();
    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    await userEvent.click(screen.getByText("support-docs"));

    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent("/knowledge/support-docs"),
    );
  });

  it("create modal posts the base", async () => {
    mockBases([]);
    const createSpy = vi.spyOn(knowledgeSdk, "createBase").mockResolvedValue(BASES[0]);

    renderPage();
    await userEvent.click(screen.getByTestId("kb-create-open"));
    await waitFor(() => expect(screen.getByTestId("kb-create-modal")).toBeInTheDocument());
    // B-175 P3-PR3 — the description box says agents read it.
    expect(screen.getByTestId("kb-create-description-hint")).toHaveTextContent(
      "it reads this description to decide which one to search",
    );
    await userEvent.type(screen.getByTestId("kb-create-name"), "support-docs");
    await userEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith(
        // B-206 K-3: the pre-filled chunk sizes are posted as-is — the platform defaults.
        expect.objectContaining({ name: "support-docs", chunkMaxTokens: 400, chunkOverlapTokens: 0 }),
      ),
    );
  });

  it("threads the ambient tenant scope into listBasesPage (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    const listSpy = mockBases([]);

    renderPage();

    await waitFor(() =>
      expect(listSpy).toHaveBeenCalledWith({ limit: 20, offset: 0 }, scopeRef.current),
    );
  });

  it("shows the empty state on the home scope", async () => {
    scopeRef.current = undefined;
    mockBases([]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("No knowledge bases yet.")).toBeInTheDocument(),
    );
  });

  it("home 态创建/删库按钮可用(两态其一)", async () => {
    mockBases(BASES);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByTestId("kb-create-open")).toBeEnabled();
    expect(screen.getByTestId("kb-delete-support-docs")).toBeEnabled();
  });

  it("切入态置灰创建/删库按钮(两态其二)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    mockBases(BASES);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByTestId("kb-create-open")).toBeDisabled();
    expect(screen.getByTestId("kb-delete-support-docs")).toBeDisabled();
  });

  it("isSupportedDocument matches the backend whitelist", () => {
    expect(knowledgeSdk.isSupportedDocument("a.PDF")).toBe(true);
    expect(knowledgeSdk.isSupportedDocument("notes.markdown")).toBe(true);
    expect(knowledgeSdk.isSupportedDocument("payload.exe")).toBe(false);
    expect(knowledgeSdk.isSupportedDocument("no-extension")).toBe(false);
  });
});

// ─── Cross-tenant W4 — "*" aggregate: tenant column + row-jump ─────────

describe("KnowledgeAdmin — cross-tenant W4 aggregate", () => {
  it("shows the tenant column and carries a foreign tenant into the row-jump", async () => {
    scopeRef.current = "*";
    mockBases([
      { ...BASES[0], tenant_id: "tenant-2-xxxx" },
    ]);

    renderPage();
    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    // Tenant column renders the truncated owning tenant.
    expect(screen.getByText("tenant-2…")).toBeInTheDocument();

    // JWT home tenant is t1; the row belongs to tenant-2 → the click
    // appends ?tenant_id= so the detail read hits the owning tenant.
    await userEvent.click(screen.getByText("support-docs"));
    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent(
        "/knowledge/support-docs?tenant_id=tenant-2-xxxx",
      ),
    );
  });

  it("row-jump keeps the plain URL for a home-tenant row", async () => {
    scopeRef.current = "*";
    mockBases([
      { ...BASES[0], tenant_id: "t1" },
    ]);

    renderPage();
    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    await userEvent.click(screen.getByText("support-docs"));
    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent("/knowledge/support-docs"),
    );
    expect(screen.getByTestId("loc")).not.toHaveTextContent("tenant_id");
  });

  // Review C-1 — writes bind by name to the caller's HOME tenant, so a
  // delete/create fired from the aggregate would hit the home tenant's
  // same-named base. Both write controls must grey out under "*".
  it("聚合态置灰创建/删库按钮(写接口按 name 绑归属租户)", async () => {
    scopeRef.current = "*";
    mockBases([
      { ...BASES[0], tenant_id: "tenant-2-xxxx" },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByTestId("kb-create-open")).toBeDisabled();
    expect(screen.getByTestId("kb-delete-support-docs")).toBeDisabled();
  });

  // rowKey must include the owning tenant: two tenants may legally own a
  // same-named base and both rows must render (a bare name key collides).
  it("renders same-named bases from two tenants as two rows", async () => {
    scopeRef.current = "*";
    mockBases([
      { ...BASES[0], tenant_id: "t1" },
      { ...BASES[0], id: "22222222-2222-2222-2222-222222222222", tenant_id: "tenant-2-xxxx" },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getAllByText("support-docs")).toHaveLength(2));
  });

  it("home scope hides the tenant column", async () => {
    scopeRef.current = undefined;
    mockBases([
      { ...BASES[0], tenant_id: "t1" },
    ]);

    renderPage();
    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.queryByText("t1…")).not.toBeInTheDocument();
  });
});

describe("KnowledgeAdmin — server-side pagination (B-175 L3)", () => {
  const many: knowledgeSdk.KnowledgeBase[] = Array.from({ length: 25 }, (_, i) => ({
    ...BASES[0],
    id: `base-${i}`,
    name: `kb-${i}`,
  }));

  function mockPaged() {
    return vi
      .spyOn(knowledgeSdk, "listBasesPage")
      .mockImplementation(async ({ limit, offset }) => ({
        bases: many.slice(offset, offset + limit),
        total: many.length,
        offset,
        limit,
      }));
  }

  it("page 2 asks the server for offset 20", async () => {
    const listSpy = mockPaged();

    renderPage();
    await waitFor(() => expect(screen.getByText("kb-0")).toBeInTheDocument());
    expect(screen.queryByText("kb-20")).not.toBeInTheDocument();
    const pager = screen.getByTestId("kb-table-pagination");
    expect(pager).toHaveTextContent(/25/);

    await userEvent.click(within(pager).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("kb-24")).toBeInTheDocument());
    expect(listSpy).toHaveBeenLastCalledWith({ limit: 20, offset: 20 }, undefined);
  });

  it("a tenant-scope switch starts again on page 1", async () => {
    const listSpy = mockPaged();

    const { rerender } = renderPage();
    await waitFor(() => expect(screen.getByText("kb-0")).toBeInTheDocument());
    await userEvent.click(within(screen.getByTestId("kb-table-pagination")).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("kb-24")).toBeInTheDocument());

    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    rerender(knowledgeTree());

    await waitFor(() =>
      expect(listSpy).toHaveBeenLastCalledWith({ limit: 20, offset: 0 }, scopeRef.current),
    );
    // never asked the new scope for the old scope's page
    expect(listSpy).not.toHaveBeenCalledWith({ limit: 20, offset: 20 }, scopeRef.current);
  });

  it("a slow response for the old scope never overwrites the new scope's rows", async () => {
    let releaseOld!: (page: knowledgeSdk.BasesPage) => void;
    const homeRows = [{ ...BASES[0], name: "home-kb" }];
    const foreignRows = [{ ...BASES[0], name: "foreign-kb" }];
    vi.spyOn(knowledgeSdk, "listBasesPage").mockImplementation((_page, scope) =>
      scope === undefined
        ? new Promise((resolve) => {
            releaseOld = resolve;
          })
        : Promise.resolve({ bases: foreignRows, total: 1, offset: 0, limit: 20 }),
    );

    const { rerender } = renderPage();
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    rerender(knowledgeTree());
    await waitFor(() => expect(screen.getByText("foreign-kb")).toBeInTheDocument());

    releaseOld({ bases: homeRows, total: 1, offset: 0, limit: 20 });
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("home-kb")).not.toBeInTheDocument();
    expect(screen.getByText("foreign-kb")).toBeInTheDocument();
  });
});
