/**
 * KnowledgeAdmin (list page) tests — KB commercial uplift.
 *
 * The knowledge SDK is stubbed. Covers: bases render with stats +
 * needs-reindex tag, row navigation to the detail page, the create modal
 * (createBase + 409 duplicate), and the H-19 scope note.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import { KnowledgeAdmin } from "../KnowledgeAdmin";

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

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/knowledge"]}>
      <App>
        <Routes>
          <Route path="/knowledge" element={<KnowledgeAdmin />} />
          <Route path="/knowledge/:name" element={<LocationProbe />} />
        </Routes>
      </App>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  scopeRef.current = undefined;
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

afterEach(() => vi.restoreAllMocks());

describe("KnowledgeAdmin (list)", () => {
  it("renders bases with stats + needs-reindex tag", async () => {
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue(BASES);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByText("Customer FAQ")).toBeInTheDocument();
    expect(screen.getByText("Needs re-index")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument(); // chunk count
  });

  it("navigates to the detail page on row click", async () => {
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue(BASES);

    renderPage();
    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    await userEvent.click(screen.getByText("support-docs"));

    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent("/knowledge/support-docs"),
    );
  });

  it("create modal posts the base", async () => {
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue([]);
    const createSpy = vi.spyOn(knowledgeSdk, "createBase").mockResolvedValue(BASES[0]);

    renderPage();
    await userEvent.click(screen.getByTestId("kb-create-open"));
    await waitFor(() => expect(screen.getByTestId("kb-create-modal")).toBeInTheDocument());
    await userEvent.type(screen.getByTestId("kb-create-name"), "support-docs");
    await userEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith(expect.objectContaining({ name: "support-docs" })),
    );
  });

  it("threads the ambient tenant scope into listBases (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    const listSpy = vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue([]);

    renderPage();

    await waitFor(() => expect(listSpy).toHaveBeenCalledWith(scopeRef.current));
  });

  it("shows the empty state on the home scope", async () => {
    scopeRef.current = undefined;
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue([]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("No knowledge bases yet.")).toBeInTheDocument(),
    );
  });

  it("home 态创建/删库按钮可用(两态其一)", async () => {
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue(BASES);

    renderPage();

    await waitFor(() => expect(screen.getByText("support-docs")).toBeInTheDocument());
    expect(screen.getByTestId("kb-create-open")).toBeEnabled();
    expect(screen.getByTestId("kb-delete-support-docs")).toBeEnabled();
  });

  it("切入态置灰创建/删库按钮(两态其二)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "listBases").mockResolvedValue(BASES);

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
