/**
 * KnowledgeDetail tests — KB commercial uplift.
 *
 * Stubs the knowledge SDK. Covers the detail shell (stats + needs-reindex
 * banner/reindex), the documents tab (localized status + re-ingest), the
 * retrieval-test tab (run → scored results), and the settings tab (PATCH).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import { KnowledgeDetail } from "../KnowledgeDetail";

// Cross-tenant W3 — the page reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const { HOME_TENANT, FOREIGN_TENANT } = vi.hoisted(() => ({
  // 归属租户(mock identity 的 homeTenantId)。
  HOME_TENANT: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  // 外租户(聚合行跳转的深链目标)。
  FOREIGN_TENANT: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
}));

// Identity mock — homeTenantId=HOME_TENANT(W4 readonly 判定要比对
// homeTenantId;真 AuthProvider 要拉 /v1/me,直接 mock useAuth 更确定)。
vi.mock("../../auth/AuthContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../auth/AuthContext")>();
  return {
    ...original,
    useAuth: () => ({
      status: "authenticated" as const,
      identity: {
        kind: "jwt" as const,
        subject: "u1",
        subjectType: "user" as const,
        homeTenantId: HOME_TENANT,
        roles: ["system_admin"],
        isSystemAdmin: true,
        homeIsPlatform: false,
        displayName: "u1",
        serverResolved: true,
      },
      token: "test-token",
      login: () => {},
      logout: () => {},
      refreshIdentity: async () => {},
    }),
  };
});

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

const BASE: knowledgeSdk.KnowledgeBase = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  chunk_max_tokens: 512,
  chunk_overlap_tokens: 64,
  created_at: "2026-06-12T00:00:00Z",
  description: "FAQ",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
  embedding_provider: "qwen",
  embedding_model: "text-embedding-v4",
  needs_reindex: true,
  reindexing: false,
  stats: { document_count: 2, chunk_count: 30 },
};

const DOCS: knowledgeSdk.KnowledgeDocument[] = [
  {
    id: "22222222-2222-2222-2222-222222222222",
    filename: "faq.pdf",
    status: "ready",
    error: null,
    chunk_count: 12,
    attempts: 1,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:05:00Z",
  },
];

function renderDetail(initial = "/knowledge/support-docs") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <App>
        <Routes>
          <Route path="/knowledge/:name" element={<KnowledgeDetail />} />
          <Route path="/knowledge/:name/:tab" element={<KnowledgeDetail />} />
        </Routes>
      </App>
    </MemoryRouter>,
  );
}

afterEach(() => vi.restoreAllMocks());

// vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
beforeEach(() => {
  isTenantSwitchedMock.mockReturnValue(false);
  scopeRef.current = undefined;
});

describe("KnowledgeDetail", () => {
  it("loads the base, shows stats + localized doc status", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail();

    await waitFor(() => expect(screen.getByTestId("knowledge-detail-root")).toBeInTheDocument());
    // findBy: the documents table lands one async hop after the shell
    // (getBase → listDocuments) — a sync getBy races it on slow runners.
    expect(await screen.findByText("faq.pdf")).toBeInTheDocument();
    // localized status (not the raw "ready").
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.queryByText("ready")).toBeNull();
  });

  it("re-index banner triggers reindexBase", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);
    const reindexSpy = vi.spyOn(knowledgeSdk, "reindexBase").mockResolvedValue();

    renderDetail();
    await waitFor(() => expect(screen.getByTestId("knowledge-needs-reindex")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("knowledge-reindex-btn"));

    await waitFor(() => expect(reindexSpy).toHaveBeenCalledWith("support-docs"));
  });

  it("re-ingest calls the SDK", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);
    const reingestSpy = vi
      .spyOn(knowledgeSdk, "reingestDocument")
      .mockResolvedValue(DOCS[0]);

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    await userEvent.click(
      screen.getByTestId("doc-reingest-22222222-2222-2222-2222-222222222222"),
    );

    await waitFor(() =>
      expect(reingestSpy).toHaveBeenCalledWith(
        "support-docs",
        "22222222-2222-2222-2222-222222222222",
      ),
    );
  });

  it("retrieval test runs the query and renders scored results", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);
    const testSpy = vi.spyOn(knowledgeSdk, "testRetrieval").mockResolvedValue({
      query: "deductible",
      count: 1,
      results: [
        {
          content: "The deductible is 500.",
          source: "faq.pdf#0",
          filename: "faq.pdf",
          chunk_index: 0,
          score: 0.91,
          recall_source: "both",
        },
      ],
    });

    renderDetail("/knowledge/support-docs/test");
    await waitFor(() => expect(screen.getByTestId("knowledge-test-tab")).toBeInTheDocument());
    await userEvent.type(screen.getByTestId("kb-test-query"), "deductible");
    await userEvent.click(screen.getByTestId("kb-test-run"));

    await waitFor(() => expect(screen.getByTestId("kb-test-results")).toBeInTheDocument());
    expect(testSpy).toHaveBeenCalledWith(
      "support-docs",
      expect.objectContaining({ query: "deductible" }),
      // W3 — trailing tenantScope is undefined in the home state.
      undefined,
    );
    expect(screen.getByText("faq.pdf#0")).toBeInTheDocument();
    expect(screen.getByText("The deductible is 500.")).toBeInTheDocument();
  });

  it("settings save calls updateBase", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);
    const updateSpy = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);

    renderDetail("/knowledge/support-docs/settings");
    await waitFor(() => expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument());
    const tab = screen.getByTestId("knowledge-settings-tab");
    await userEvent.click(within(tab).getByTestId("kb-settings-save"));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("support-docs", expect.any(Object)));
  });

  it("切入态置灰重建索引/文档删除(两态:home 态由上方用例覆盖)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());

    expect(screen.getByTestId("knowledge-reindex-btn")).toBeDisabled();
    expect(
      screen.getByTestId("doc-delete-22222222-2222-2222-2222-222222222222"),
    ).toBeDisabled();
  });

  it("切入态置灰设置保存(两态:home 态由上方用例覆盖)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail("/knowledge/support-docs/settings");
    await waitFor(() => expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument());

    const tab = screen.getByTestId("knowledge-settings-tab");
    expect(within(tab).getByTestId("kb-settings-save")).toBeDisabled();
    expect(within(tab).getByTestId("kb-settings-reindex")).toBeDisabled();
  });

  it("URL ?tenant_id= wins over the ambient scope for the base read (W4)", async () => {
    // Ambient scope is a switched-in tenant; the aggregate row-jump's URL
    // param must still win (it names the row's owning tenant).
    scopeRef.current = "33333333-3333-3333-3333-333333333333";
    const getBaseSpy = vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail("/knowledge/support-docs?tenant_id=tenant-2-xxxx");
    await waitFor(() =>
      expect(getBaseSpy).toHaveBeenCalledWith("support-docs", "tenant-2-xxxx"),
    );
  });
});

// ─── Cross-tenant W4(review C-2)— 子 tab readScope 接线 + readonly 跟随读目标 ───

describe("KnowledgeDetail — cross-tenant W4 (review C-2)", () => {
  it("deep-link ?tenant_id= threads into the documents read (readScope prop)", async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const listSpy = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail(`/knowledge/support-docs?tenant_id=${FOREIGN_TENANT}`);

    // 把 DocumentsTab 的 readScope prop 换回 ambient("*" 折叠 undefined)
    // 这里必须红。
    await waitFor(() =>
      expect(listSpy).toHaveBeenCalledWith("support-docs", FOREIGN_TENANT),
    );
  });

  it('"*" 聚合 + 外租户深链 → 写控件置灰(readonly 跟随读目标)', async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail(`/knowledge/support-docs?tenant_id=${FOREIGN_TENANT}`);
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());

    // 页面级 reindex + DocumentsTab 的删除各断一处(写接口按 name 绑归属
    // 租户,点了会误伤 home 同名库)。
    expect(screen.getByTestId("knowledge-reindex-btn")).toBeDisabled();
    expect(
      screen.getByTestId("doc-delete-22222222-2222-2222-2222-222222222222"),
    ).toBeDisabled();
  });

  it('"*" 聚合无深链(读归属租户)→ 保持可写(防"一律置灰"退化)', async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());

    expect(screen.getByTestId("knowledge-reindex-btn")).toBeEnabled();
    expect(
      screen.getByTestId("doc-delete-22222222-2222-2222-2222-222222222222"),
    ).toBeEnabled();
  });

  it("settings 外租户深链置灰保存/重建索引(readonly prop 下传)", async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);

    renderDetail(`/knowledge/support-docs/settings?tenant_id=${FOREIGN_TENANT}`);
    await waitFor(() =>
      expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument(),
    );

    const tab = screen.getByTestId("knowledge-settings-tab");
    expect(within(tab).getByTestId("kb-settings-save")).toBeDisabled();
    expect(within(tab).getByTestId("kb-settings-reindex")).toBeDisabled();
  });

  it("换 tab 保留 ?tenant_id=:documents→test 后检索测试仍读外租户(MUT-11)", async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(DOCS);
    const testSpy = vi.spyOn(knowledgeSdk, "testRetrieval").mockResolvedValue({
      query: "q",
      count: 0,
      results: [],
    });

    renderDetail(`/knowledge/support-docs?tenant_id=${FOREIGN_TENANT}`);
    await waitFor(() => expect(screen.getByTestId("knowledge-detail-root")).toBeInTheDocument());

    // Tabs onChange 丢掉 ``?tenant_id=`` 保留(MUT-11)→ readScope 落回
    // ambient("*" 折叠 undefined)→ 这里读到 undefined → 红。
    await userEvent.click(screen.getByRole("tab", { name: "Retrieval test" }));
    await waitFor(() => expect(screen.getByTestId("knowledge-test-tab")).toBeInTheDocument());
    await userEvent.type(screen.getByTestId("kb-test-query"), "q");
    await userEvent.click(screen.getByTestId("kb-test-run"));

    await waitFor(() =>
      expect(testSpy).toHaveBeenCalledWith(
        "support-docs",
        expect.objectContaining({ query: "q" }),
        FOREIGN_TENANT,
      ),
    );
  });
});
