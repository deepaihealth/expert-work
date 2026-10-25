/**
 * KnowledgeDetail tests — KB commercial uplift.
 *
 * Stubs the knowledge SDK. Covers the detail shell (stats + needs-reindex
 * banner/reindex), the documents tab (localized status + re-ingest), the
 * retrieval-test tab (run → scored results), and the settings tab (PATCH).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import i18n from "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { KnowledgeDetail } from "../KnowledgeDetail";
import { DocumentsTab } from "../knowledge_detail/DocumentsTab";

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

/** Cases that render a full 50-row page more than once: ~1.8 s locally,
 *  3–6 s on the CI runner — over vitest's 5 s default. */
const FULL_PAGE_TIMEOUT_MS = 15_000;

/** Click a Popconfirm's OK button — it shares the trigger's label and
 *  mounts last (portal). */
async function confirmPopover(label: string) {
  const buttons = await screen.findAllByRole("button", { name: label });
  await userEvent.click(buttons[buttons.length - 1]);
}

/** A one-page ``listDocuments`` response. */
function docPage(
  documents: knowledgeSdk.KnowledgeDocument[],
  total = documents.length,
  offset = 0,
): knowledgeSdk.DocumentsPage {
  return { documents, total, offset, limit: 50 };
}

function detailTree(initial: string) {
  return (
    <MemoryRouter initialEntries={[initial]}>
      <App>
        <Routes>
          <Route path="/knowledge/:name" element={<KnowledgeDetail />} />
          <Route path="/knowledge/:name/:tab" element={<KnowledgeDetail />} />
        </Routes>
      </App>
    </MemoryRouter>
  );
}

function renderDetail(initial = "/knowledge/support-docs") {
  return render(detailTree(initial));
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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const reindexSpy = vi.spyOn(knowledgeSdk, "reindexBase").mockResolvedValue();

    renderDetail();
    await waitFor(() => expect(screen.getByTestId("knowledge-needs-reindex")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("knowledge-reindex-btn"));

    await waitFor(() => expect(reindexSpy).toHaveBeenCalledWith("support-docs"));
  });

  it("re-ingest calls the SDK", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
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

  it("shows scanned-page counts and recognizes failed / waiting pages (B-209)", async () => {
    const scanned: knowledgeSdk.KnowledgeDocument = {
      ...DOCS[0],
      id: "33333333-3333-3333-3333-333333333333",
      filename: "handbook.pdf",
      page_stats: {
        pages_total: 12,
        pages_ocr: 3,
        pages_failed: 1,
        pages_pending: 2,
        pages_over_limit: 0,
      },
    };
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([DOCS[0], scanned]));
    const recognizeSpy = vi.spyOn(knowledgeSdk, "recognizeDocument").mockResolvedValue(scanned);

    renderDetail();
    const tags = await screen.findByTestId(`doc-pages-${scanned.id}`);
    expect(tags).toHaveTextContent("3 pages recognized");
    expect(tags).toHaveTextContent("1 pages failed");
    expect(tags).toHaveTextContent("2 pages waiting");
    expect(tags).not.toHaveTextContent("over the limit");
    // A document without scanned pages shows no counts and no 「补识别」.
    expect(screen.queryByTestId(`doc-pages-${DOCS[0].id}`)).toBeNull();
    expect(screen.queryByTestId(`doc-recognize-${DOCS[0].id}`)).toBeNull();

    await userEvent.click(screen.getByTestId(`doc-recognize-${scanned.id}`));
    await waitFor(() =>
      expect(recognizeSpy).toHaveBeenCalledWith("support-docs", scanned.id),
    );
  });

  it("offers 「补识别」 for pages over the limit (B-209 review)", async () => {
    const capped: knowledgeSdk.KnowledgeDocument = {
      ...DOCS[0],
      page_stats: {
        pages_total: 5,
        pages_ocr: 2,
        pages_failed: 0,
        pages_pending: 0,
        pages_over_limit: 3,
      },
    };
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([capped]));

    renderDetail();
    expect(await screen.findByTestId(`doc-pages-${capped.id}`)).toHaveTextContent(
      "3 pages over the limit",
    );
    expect(screen.getByTestId(`doc-recognize-${capped.id}`)).toBeInTheDocument();
  });

  it("offers no 「补识别」 when every scanned page was recognized", async () => {
    const done: knowledgeSdk.KnowledgeDocument = {
      ...DOCS[0],
      page_stats: {
        pages_total: 4,
        pages_ocr: 4,
        pages_failed: 0,
        pages_pending: 0,
        pages_over_limit: 0,
      },
    };
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([done]));

    renderDetail();
    expect(await screen.findByTestId(`doc-pages-${done.id}`)).toHaveTextContent(
      "4 pages recognized",
    );
    expect(screen.queryByTestId(`doc-recognize-${done.id}`)).toBeNull();
  });

  it("shows picture counts and offers 「补识别」 for pictures waiting (B-209 PR-2)", async () => {
    const pictured: knowledgeSdk.KnowledgeDocument = {
      ...DOCS[0],
      id: "44444444-4444-4444-4444-444444444444",
      filename: "manual.docx",
      figure_stats: {
        extracted: 6,
        filtered: 2,
        capped: 0,
        unsupported: 1,
        described: 2,
        failed: 0,
        pending: 1,
      },
    };
    const allDescribed: knowledgeSdk.KnowledgeDocument = {
      ...DOCS[0],
      figure_stats: { ...pictured.figure_stats!, described: 3, pending: 0 },
    };
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(
      docPage([allDescribed, pictured]),
    );
    const recognizeSpy = vi.spyOn(knowledgeSdk, "recognizeDocument").mockResolvedValue(pictured);

    renderDetail();
    const tags = await screen.findByTestId(`doc-figures-${pictured.id}`);
    expect(tags).toHaveTextContent("2 pictures described");
    expect(tags).toHaveTextContent("1 pictures waiting");
    expect(tags).not.toHaveTextContent("failed");
    expect(screen.getByTestId(`doc-figures-${allDescribed.id}`)).toHaveTextContent(
      "3 pictures described",
    );
    // Every picture described: nothing for 「补识别」 to do.
    expect(screen.queryByTestId(`doc-recognize-${allDescribed.id}`)).toBeNull();
    await userEvent.click(screen.getByTestId(`doc-recognize-${pictured.id}`));
    await waitFor(() =>
      expect(recognizeSpy).toHaveBeenCalledWith("support-docs", pictured.id),
    );
  });

  it("segment preview shows the original next to a picture description (B-209 PR-2)", async () => {
    const figure = "0123456789abcdef";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "listChunks").mockResolvedValue({
      chunks: [
        {
          id: "c1",
          chunk_index: 0,
          content: `Before the picture.\n\n> \u3014\u56fe ${figure}\u3015A device photo.\n\nAfter it.`,
          heading_path: [],
        },
        { id: "c2", chunk_index: 1, content: "No picture here.", heading_path: [] },
      ],
      total: 2,
      offset: 0,
      limit: 20,
      figure_ids: [figure],
    });
    const fetchSpy = vi.spyOn(knowledgeSdk, "fetchFigureUrl").mockResolvedValue("blob:figure");

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-chunks-${DOCS[0].id}`));
    const block = await screen.findByTestId(`kb-figure-block-${figure}`);
    expect(block).toHaveTextContent("A device photo.");
    const thumb = await screen.findByTestId(`kb-figure-thumb-${figure}`);
    expect(within(thumb).getByRole("img", { name: "Original picture" })).toHaveAttribute(
      "src",
      "blob:figure",
    );
    expect(screen.getByText("Before the picture.")).toBeInTheDocument();
    expect(screen.getByText("After it.")).toBeInTheDocument();
    // Only the described picture is fetched — once, under its own document.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith("support-docs", DOCS[0].id, figure, undefined);
  });

  it("segment preview says when the original is not available (B-209 PR-2)", async () => {
    const figure = "fedcba9876543210";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "listChunks").mockResolvedValue({
      chunks: [
        {
          id: "c1",
          chunk_index: 0,
          content: `> \u3014\u56fe ${figure}\u3015A chart.`,
          heading_path: [],
        },
      ],
      total: 1,
      offset: 0,
      limit: 20,
      figure_ids: [figure],
    });
    vi.spyOn(knowledgeSdk, "fetchFigureUrl").mockRejectedValue(new Error("404"));

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-chunks-${DOCS[0].id}`));
    expect(await screen.findByTestId(`kb-figure-missing-${figure}`)).toHaveTextContent(
      "Original not available",
    );
  });

  it("segment preview shows thumbnails only for pictures the server confirmed (S-L1)", async () => {
    const real = "0123456789abcdef";
    const typed = "fedcba9876543210";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "listChunks").mockResolvedValue({
      chunks: [
        {
          id: "c1",
          chunk_index: 0,
          content: `> \u3014\u56fe ${real}\u3015A photo.\n\n> \u3014\u56fe ${typed}\u3015Typed by an author.`,
          heading_path: [],
        },
      ],
      total: 1,
      offset: 0,
      limit: 20,
      figure_ids: [real],
    });
    const fetchSpy = vi.spyOn(knowledgeSdk, "fetchFigureUrl").mockResolvedValue("blob:figure");

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-chunks-${DOCS[0].id}`));
    expect(await screen.findByTestId(`kb-figure-thumb-${real}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`kb-figure-block-${typed}`)).toBeNull();
    expect(screen.getByText(/Typed by an author\./)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith("support-docs", DOCS[0].id, real, undefined);
  });

  it("retrieval test runs the query and renders scored results", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
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
          heading_path: ["Benefits", "Deductible"],
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
    expect(screen.getByTestId("kb-test-heading-0")).toHaveTextContent("Benefits › Deductible");
  });

  it("segment preview shows each chunk's heading path above its body", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "listChunks").mockResolvedValue({
      chunks: [
        { id: "c1", chunk_index: 0, content: "500 per year.", heading_path: ["Benefits"] },
        // A legacy chunk: prefix still in the body, no path → no breadcrumb row.
        { id: "c2", chunk_index: 1, content: "[Section: Old]\n\nlegacy body", heading_path: [] },
      ],
      total: 2,
      offset: 0,
      limit: 20,
    });

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-chunks-${DOCS[0].id}`));
    expect(await screen.findByTestId("kb-chunk-heading-c1")).toHaveTextContent("Benefits");
    expect(screen.getByText("500 per year.")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-chunk-heading-c2")).not.toBeInTheDocument();
  });

  describe("segment preview views (B-206 K-1)", () => {
    const CHILDREN: knowledgeSdk.ChunksPage = {
      chunks: [
        { id: "c1", chunk_index: 0, content: "alpha", chunk_type: "text", parent_index: 3 },
        { id: "c2", chunk_index: 1, content: "beta", chunk_type: "text", parent_index: null },
      ],
      total: 2,
      offset: 0,
      limit: 20,
    };
    const PARENTS: knowledgeSdk.ChunksPage = {
      chunks: [
        {
          id: "p1",
          chunk_index: 3,
          content: "alpha and more",
          chunk_type: "parent",
          heading_path: ["Benefits"],
          parent_index: null,
        },
      ],
      total: 1,
      offset: 0,
      limit: 20,
    };

    function byView(parents: knowledgeSdk.ChunksPage = PARENTS) {
      return vi
        .spyOn(knowledgeSdk, "listChunks")
        .mockImplementation(async (_base, _doc, params) =>
          params?.view === "parents" ? parents : CHILDREN,
        );
    }

    async function inChinese(body: () => Promise<void>): Promise<void> {
      const prior = i18n.language;
      await i18n.changeLanguage("zh-CN");
      try {
        await body();
      } finally {
        await i18n.changeLanguage(prior);
      }
    }

    async function openDrawer(): Promise<void> {
      vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
      vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
      renderDetail();
      await userEvent.click(await screen.findByTestId(`doc-chunks-${DOCS[0].id}`));
    }

    it("lists chunks first, each naming the 整块 it belongs to", async () => {
      await inChinese(async () => {
        const spy = byView();
        await openDrawer();
        expect(await screen.findByText("alpha")).toBeInTheDocument();
        expect(spy).toHaveBeenLastCalledWith(
          "support-docs",
          DOCS[0].id,
          { offset: 0, limit: 20, view: "children" },
          undefined,
        );
        expect(screen.getByRole("radio", { name: "按小段" })).toBeChecked();
        expect(screen.getByText("小段 #0")).toBeInTheDocument();
        expect(screen.getByTestId("kb-chunk-parent-c1")).toHaveTextContent("属于整块 #3");
        // A chunk with no parent row says nothing about one.
        expect(screen.getByText("小段 #1")).toBeInTheDocument();
        expect(screen.queryByTestId("kb-chunk-parent-c2")).toBeNull();
      });
    });

    it("switches to the 整块 and back", async () => {
      await inChinese(async () => {
        const spy = byView();
        await openDrawer();
        await screen.findByText("alpha");
        await userEvent.click(screen.getByText("按整块"));
        expect(await screen.findByText("alpha and more")).toBeInTheDocument();
        expect(spy).toHaveBeenLastCalledWith(
          "support-docs",
          DOCS[0].id,
          { offset: 0, limit: 20, view: "parents" },
          undefined,
        );
        // A parent card reads like a chunk card: its label, breadcrumb and text.
        expect(screen.getByText("整块 #3")).toBeInTheDocument();
        expect(screen.getByTestId("kb-chunk-heading-p1")).toHaveTextContent("Benefits");
        expect(screen.queryByText("alpha")).toBeNull();
        expect(screen.queryByTestId("kb-chunk-parent-p1")).toBeNull();
        await userEvent.click(screen.getByText("按小段"));
        expect(await screen.findByText("alpha")).toBeInTheDocument();
        expect(screen.queryByText("alpha and more")).toBeNull();
      });
    });

    it("pages the 整块 view by its own total", async () => {
      const spy = byView({ ...PARENTS, total: 45 });
      await openDrawer();
      await screen.findByText("alpha");
      await userEvent.click(screen.getByText("By passage"));
      await screen.findByText("alpha and more");
      const drawer = screen.getByTestId("kb-chunks-drawer");
      await userEvent.click(within(drawer).getByText("2"));
      await waitFor(() =>
        expect(spy).toHaveBeenLastCalledWith(
          "support-docs",
          DOCS[0].id,
          { offset: 20, limit: 20, view: "parents" },
          undefined,
        ),
      );
    });

    it("says a document with no 整块 has none", async () => {
      await inChinese(async () => {
        byView({ chunks: [], total: 0, offset: 0, limit: 20 });
        await openDrawer();
        await screen.findByText("alpha");
        await userEvent.click(screen.getByText("按整块"));
        expect(
          await screen.findByText("这个文档没有整块，检索时每个小段单独作为一条结果。"),
        ).toBeInTheDocument();
      });
    });

    it("a slower answer for the view just left never replaces the one asked for", async () => {
      let answerChildren: (page: knowledgeSdk.ChunksPage) => void = () => {};
      vi.spyOn(knowledgeSdk, "listChunks").mockImplementation((_base, _doc, params) =>
        params?.view === "parents"
          ? Promise.resolve(PARENTS)
          : new Promise((resolve) => {
              answerChildren = resolve;
            }),
      );
      await openDrawer();
      await userEvent.click(await screen.findByText("By passage"));
      expect(await screen.findByText("alpha and more")).toBeInTheDocument();
      await act(async () => answerChildren(CHILDREN));
      expect(screen.getByText("alpha and more")).toBeInTheDocument();
      expect(screen.queryByText("alpha")).toBeNull();
    });

    it("opens the next document on the chunk view again", async () => {
      const spy = byView();
      await openDrawer();
      await screen.findByText("alpha");
      await userEvent.click(screen.getByText("By passage"));
      await screen.findByText("alpha and more");
      await userEvent.click(screen.getByRole("button", { name: "Close" }));
      await userEvent.click(screen.getByTestId(`doc-chunks-${DOCS[0].id}`));
      await waitFor(() =>
        expect(spy).toHaveBeenLastCalledWith(
          "support-docs",
          DOCS[0].id,
          { offset: 0, limit: 20, view: "children" },
          undefined,
        ),
      );
      expect(await screen.findByText("alpha")).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: "By chunk" })).toBeChecked();
    });
  });

  it("settings save calls updateBase", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const updateSpy = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);

    renderDetail("/knowledge/support-docs/settings");
    await waitFor(() => expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument());
    const tab = screen.getByTestId("knowledge-settings-tab");
    await userEvent.click(within(tab).getByTestId("kb-settings-save"));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("support-docs", expect.any(Object)));
  });

  it("settings save sends an edited rerank score floor (B-175 wave 2 PR3; B-206 K-7b)", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue({
      ...BASE,
      retrieval_config: { ...BASE.retrieval_config!, rerank_score_threshold: 0.7 },
    });
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const updateSpy = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);

    renderDetail("/knowledge/support-docs/settings");
    await waitFor(() => expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument());
    const tab = screen.getByTestId("knowledge-settings-tab");
    const input = within(tab).getByTestId("kb-settings-rerank-threshold");
    expect(input).toHaveAttribute("aria-valuenow", "0.7");
    // B-206 K-7b — sent only when edited: sending binds it to the current rerank model.
    await userEvent.clear(input);
    await userEvent.type(input, "0.65");
    await userEvent.click(within(tab).getByTestId("kb-settings-save"));

    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith(
        "support-docs",
        expect.objectContaining({ rerank_score_threshold: 0.65 }),
      ),
    );
  });

  it("rerank score floor has no default — empty, with a reference placeholder (B-175 wave 2 PR3)", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const updateSpy = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);

    renderDetail("/knowledge/support-docs/settings");
    await waitFor(() => expect(screen.getByTestId("knowledge-settings-tab")).toBeInTheDocument());
    const tab = screen.getByTestId("knowledge-settings-tab");
    const input = within(tab).getByTestId("kb-settings-rerank-threshold");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Empty = off (e.g. 0.70)");
    await userEvent.click(within(tab).getByTestId("kb-settings-save"));

    // Untouched, the floor is left out of the save (B-206 K-7b) — it stays off.
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    expect(updateSpy.mock.calls[0][1]).not.toHaveProperty("rerank_score_threshold");
  });

  it("切入态置灰重建索引/文档删除(两态:home 态由上方用例覆盖)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

    renderDetail("/knowledge/support-docs?tenant_id=tenant-2-xxxx");
    await waitFor(() =>
      expect(getBaseSpy).toHaveBeenCalledWith("support-docs", "tenant-2-xxxx"),
    );
  });

  it("a superseded read that fails late does not override the current one (B-183)", async () => {
    // The read scope changes under a read in flight (home → switched-in
    // tenant). The first read 404s *after* the second one succeeded; the
    // page must stay on the second one, not end on the error card.
    const SWITCHED = "dd068302-0000-4000-8000-000000000001";
    let failFirst: (err: unknown) => void = () => {};
    const getBaseSpy = vi
      .spyOn(knowledgeSdk, "getBase")
      .mockImplementation((_name, scope) =>
        scope === SWITCHED
          ? Promise.resolve(BASE)
          : new Promise((_res, rej) => {
              failFirst = rej;
            }),
      );
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

    const view = renderDetail("/knowledge/support-docs/documents");
    await waitFor(() => expect(getBaseSpy).toHaveBeenCalledWith("support-docs", undefined));

    scopeRef.current = SWITCHED;
    view.rerender(detailTree("/knowledge/support-docs/documents"));
    await waitFor(() => expect(screen.getByTestId("knowledge-detail-root")).toBeInTheDocument());

    await act(async () => {
      failFirst(new ApiError("knowledge base not found", "HTTP_404", 404));
    });
    expect(screen.queryByTestId("knowledge-detail-error")).toBeNull();
    expect(screen.getByTestId("knowledge-detail-root")).toBeInTheDocument();
  });
});

// ─── Cross-tenant W4(review C-2)— 子 tab readScope 接线 + readonly 跟随读目标 ───

describe("KnowledgeDetail — cross-tenant W4 (review C-2)", () => {
  it("deep-link ?tenant_id= threads into the documents read (readScope prop)", async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const listSpy = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

    renderDetail(`/knowledge/support-docs?tenant_id=${FOREIGN_TENANT}`);

    // 把 DocumentsTab 的 readScope prop 换回 ambient("*" 折叠 undefined)
    // 这里必须红。
    await waitFor(() =>
      expect(listSpy).toHaveBeenCalledWith(
        "support-docs",
        { limit: 50, offset: 0 },
        FOREIGN_TENANT,
      ),
    );
  });

  it('"*" 聚合 + 外租户深链 → 写控件置灰(readonly 跟随读目标)', async () => {
    scopeRef.current = "*";
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

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
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
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

// ---------------------------------------------------------------------------
// B-175 L3 — upload queue (batch / folder import) + client-side pagination.
// ---------------------------------------------------------------------------

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fileOf(name: string, size?: number): File {
  const file = new File(["abc"], name);
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

const MIB = 1024 * 1024;

/** 6 picks: 4 supported + 1 ``.exe`` + 1 oversized → with concurrency 3 the
 *  queue reads 3 uploading / 1 queued / 2 skipped. */
const BATCH = [
  fileOf("a.pdf"),
  fileOf("setup.exe"),
  fileOf("b.docx"),
  fileOf("huge.pdf", 60 * MIB),
  fileOf("c.md"),
  fileOf("d.txt"),
];

function pickFiles(files: File[]) {
  // rc-upload's hidden <input type=file> carries the dragger's data-testid.
  fireEvent.change(screen.getByTestId("doc-upload-dragger"), { target: { files } });
}

function rowStatuses(): string[] {
  return screen
    .getAllByTestId(/^doc-upload-queue-row-/)
    .map((row) => row.getAttribute("data-status") ?? "");
}

function countBy(values: string[]): Record<string, number> {
  return values.reduce<Record<string, number>>(
    (acc, v) => ({ ...acc, [v]: (acc[v] ?? 0) + 1 }),
    {},
  );
}

describe("DocumentsTab — upload queue (B-175 L3)", () => {
  it("a 6-file pick reads 3 uploading / 1 queued / 2 skipped and refreshes once after settling", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const listSpy = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const pending: Deferred<knowledgeSdk.KnowledgeDocument>[] = [];
    const uploadSpy = vi.spyOn(knowledgeSdk, "uploadDocument").mockImplementation(() => {
      const d = deferred<knowledgeSdk.KnowledgeDocument>();
      pending.push(d);
      return d.promise;
    });

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    const listCallsBefore = listSpy.mock.calls.length;

    pickFiles(BATCH);

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(3));
    expect(uploadSpy.mock.calls.map((c) => c[1].name)).toEqual(["a.pdf", "b.docx", "c.md"]);
    // signal + onProgress are threaded through to the SDK
    expect(uploadSpy.mock.calls[0][2]).toEqual(
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        onProgress: expect.any(Function),
      }),
    );
    expect(screen.getByTestId("doc-upload-queue")).toBeInTheDocument();
    expect(countBy(rowStatuses())).toEqual({ uploading: 3, queued: 1 });
    const summary = screen.getByTestId("doc-upload-summary");
    expect(summary).toHaveTextContent(/1 unsupported/);
    expect(summary).toHaveTextContent(/1 over 50\.0 MB/);
    // the skipped rows live behind the summary's expand toggle
    await userEvent.click(within(summary).getByRole("button", { name: /show list/i }));
    expect(countBy(rowStatuses())).toEqual({ uploading: 3, queued: 1, skipped: 2 });
    // nothing settled yet → no refresh
    expect(listSpy).toHaveBeenCalledTimes(listCallsBefore);

    await act(async () => {
      pending[0].resolve(DOCS[0]);
      pending[1].resolve(DOCS[0]);
      pending[2].resolve(DOCS[0]);
    });
    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(4));
    expect(uploadSpy.mock.calls[3][1].name).toBe("d.txt");
    expect(listSpy).toHaveBeenCalledTimes(listCallsBefore);

    await act(async () => {
      pending[3].resolve(DOCS[0]);
    });
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(listCallsBefore + 1));
    expect(countBy(rowStatuses())).toEqual({ done: 4, skipped: 2 });
    // and no second refresh trails in
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(listSpy).toHaveBeenCalledTimes(listCallsBefore + 1);
  });

  it("a server 413 lands the row in failed with the server's limit; retry re-sends", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const uploadSpy = vi
      .spyOn(knowledgeSdk, "uploadDocument")
      .mockRejectedValueOnce(
        new ApiError("Request failed with status code 413", "HTTP_413", 413, {
          error: "file_too_large",
          limit_bytes: 10 * MIB,
        }),
      )
      .mockResolvedValue(DOCS[0]);

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());

    pickFiles([fileOf("a.pdf")]);

    await waitFor(() => expect(countBy(rowStatuses())).toEqual({ failed: 1 }));
    const row = screen.getAllByTestId(/^doc-upload-queue-row-/)[0];
    expect(row).toHaveTextContent(/10\.0 MB/);

    await userEvent.click(within(row).getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(countBy(rowStatuses())).toEqual({ done: 1 }));
  });

  it("cancel aborts the in-flight upload", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const signals: AbortSignal[] = [];
    vi.spyOn(knowledgeSdk, "uploadDocument").mockImplementation((_b, _f, opts) => {
      if (opts?.signal) signals.push(opts.signal);
      return new Promise(() => {});
    });

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    pickFiles([fileOf("a.pdf")]);
    await waitFor(() => expect(signals).toHaveLength(1));

    const row = screen.getAllByTestId(/^doc-upload-queue-row-/)[0];
    // per-row controls are labelled with the file's path (review #11)
    await userEvent.click(within(row).getByRole("button", { name: "Cancel a.pdf" }));

    expect(signals[0].aborted).toBe(true);
    await waitFor(() => expect(countBy(rowStatuses())).toEqual({ failed: 1 }));
    // the header counts it as cancelled, not failed (review #9)
    const header = screen.getByTestId("doc-upload-queue-header");
    expect(header).toHaveTextContent(/Cancelled 1/);
    expect(header).not.toHaveTextContent(/Failed/);
    // "Clear finished" dismisses the cancelled row (review #9)
    await userEvent.click(within(header).getByRole("button", { name: /clear finished/i }));
    expect(screen.queryByTestId("doc-upload-queue")).not.toBeInTheDocument();
  });

  it("a server 413 without limit_bytes says 'too large' without inventing a number (review #8)", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "uploadDocument").mockRejectedValue(
      new ApiError("Request failed with status code 413", "HTTP_413", 413),
    );

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    pickFiles([fileOf("a.pdf")]);

    await waitFor(() => expect(countBy(rowStatuses())).toEqual({ failed: 1 }));
    const row = screen.getAllByTestId(/^doc-upload-queue-row-/)[0];
    expect(row).toHaveTextContent(/file too large/i);
    expect(row).not.toHaveTextContent(/MB/);
  });

  it("readonly after the fact: Retry on a failed row is disabled and never re-sends (review #3)", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const uploadSpy = vi
      .spyOn(knowledgeSdk, "uploadDocument")
      .mockRejectedValue(new Error("boom"));

    const view = renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    pickFiles([fileOf("a.pdf")]);
    await waitFor(() => expect(countBy(rowStatuses())).toEqual({ failed: 1 }));
    expect(uploadSpy).toHaveBeenCalledTimes(1);

    // the page flips to readonly (e.g. the admin switches tenant) while the
    // failed row is still on screen
    isTenantSwitchedMock.mockReturnValue(true);
    view.rerender(detailTree("/knowledge/support-docs"));
    await waitFor(() => expect(screen.getByTestId("doc-upload-dragger")).toBeDisabled());

    const row = screen.getAllByTestId(/^doc-upload-queue-row-/)[0];
    const retry = within(row).getByRole("button", { name: "Retry a.pdf" });
    expect(retry).toBeDisabled();
    fireEvent.click(retry);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(countBy(rowStatuses())).toEqual({ failed: 1 });
  });

  it("a pick whose name matches an existing document uploads as an update; unchanged bytes say so", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const uploadSpy = vi
      .spyOn(knowledgeSdk, "uploadDocument")
      .mockResolvedValue({ ...DOCS[0], unchanged: true });

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    pickFiles([fileOf("faq.pdf")]); // DOCS[0].filename

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(1));
    expect(uploadSpy.mock.calls[0][1].name).toBe("faq.pdf");
    expect(await screen.findByTestId(/^doc-upload-unchanged-/)).toHaveTextContent(
      /Unchanged/,
    );
    expect(screen.queryByTestId("doc-upload-summary")).not.toBeInTheDocument();
  });

  it("a dropped folder is expanded and its unsupported files are counted as skipped (review #4)", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const uploadSpy = vi
      .spyOn(knowledgeSdk, "uploadDocument")
      .mockImplementation(() => new Promise(() => {}));

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());

    // Minimal FileSystem*Entry fakes, shaped as rc-upload's traverseFileTree
    // consumes them: a directory "docs" holding a.pdf and setup.exe.
    const fileEntry = (name: string) => ({
      isFile: true,
      isDirectory: false,
      fullPath: `/docs/${name}`,
      file: (cb: (f: File) => void) => cb(fileOf(name)),
    });
    const children = [fileEntry("a.pdf"), fileEntry("setup.exe")];
    const dirEntry = {
      isFile: false,
      isDirectory: true,
      fullPath: "/docs",
      createReader: () => {
        let drained = false;
        return {
          readEntries: (ok: (entries: unknown[]) => void) => {
            if (drained) return ok([]);
            drained = true;
            return ok(children);
          },
        };
      },
    };
    const dataTransfer = {
      items: [{ kind: "file", webkitGetAsEntry: () => dirEntry }],
      files: [],
      types: ["Files"],
    };

    const input = screen.getByTestId("doc-upload-dragger");
    // dragover flips the Dragger to directory mode so rc-upload walks entries
    fireEvent.dragOver(input, { dataTransfer });
    expect(input).toHaveAttribute("webkitdirectory");
    fireEvent.drop(input, { dataTransfer });

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(1));
    expect(uploadSpy.mock.calls[0][1].name).toBe("a.pdf");
    expect(screen.getByText("docs/a.pdf")).toBeInTheDocument();
    const summary = screen.getByTestId("doc-upload-summary");
    expect(summary).toHaveTextContent(/Skipped 1 unsupported file/);
    await userEvent.click(within(summary).getByRole("button", { name: /show list/i }));
    expect(countBy(rowStatuses())).toEqual({ uploading: 1, skipped: 1 });
    expect(screen.getByText("docs/setup.exe")).toBeInTheDocument();
    // the drop ended the drag → back to file mode
    expect(input).not.toHaveAttribute("webkitdirectory");
  });

  it("readonly (switched tenant): a pick never reaches the SDK and no queue renders", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));
    const uploadSpy = vi.spyOn(knowledgeSdk, "uploadDocument").mockResolvedValue(DOCS[0]);

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    expect(screen.getByTestId("doc-upload-dragger")).toBeDisabled();

    // fireEvent bypasses the disabled attribute — proves enqueue itself is a no-op.
    pickFiles([fileOf("a.pdf"), fileOf("b.pdf")]);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });

    expect(uploadSpy).not.toHaveBeenCalled();
    expect(screen.queryByTestId("doc-upload-queue")).not.toBeInTheDocument();
  });

  it("folder toggle flips the input to directory mode", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    const input = screen.getByTestId("doc-upload-dragger");
    expect(input).not.toHaveAttribute("webkitdirectory");
    // No ``accept`` on any path: rc-upload would filter unsupported files
    // before the queue can count them (antd defaults the attr to "", which
    // attrAccept treats as "accept all").
    expect(input.getAttribute("accept") ?? "").toBe("");

    await userEvent.click(screen.getByTestId("doc-upload-folder-toggle"));

    const dirInput = screen.getByTestId("doc-upload-dragger");
    expect(dirInput).toHaveAttribute("webkitdirectory");
    expect(dirInput.getAttribute("accept") ?? "").toBe("");
  });

  it("documents table pages server-side: page 2 asks for offset 50", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...DOCS[0],
      id: `33333333-3333-3333-3333-${String(i).padStart(12, "0")}`,
      filename: `doc-${i}.pdf`,
    }));
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockImplementation(async (_base, { limit, offset }) =>
        docPage(many.slice(offset, offset + limit), many.length, offset),
      );

    renderDetail();
    await waitFor(() => expect(screen.getByText("doc-0.pdf")).toBeInTheDocument());

    expect(listSpy).toHaveBeenLastCalledWith("support-docs", { limit: 50, offset: 0 }, undefined);
    expect(screen.getAllByText(/^doc-\d+\.pdf$/)).toHaveLength(50);
    const pager = screen.getByTestId("doc-table-pagination");
    expect(pager).toHaveTextContent(/60/);
    await userEvent.click(within(pager).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("doc-59.pdf")).toBeInTheDocument());
    expect(listSpy).toHaveBeenLastCalledWith("support-docs", { limit: 50, offset: 50 }, undefined);
    expect(screen.getAllByText(/^doc-\d+\.pdf$/)).toHaveLength(10);
  }, FULL_PAGE_TIMEOUT_MS);

  it("a page emptied under us steps back to the last page with rows", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const many = Array.from({ length: 51 }, (_, i) => ({
      ...DOCS[0],
      id: `44444444-4444-4444-4444-${String(i).padStart(12, "0")}`,
      filename: `doc-${i}.pdf`,
    }));
    let rows = many;
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockImplementation(async (_base, { limit, offset }) =>
        docPage(rows.slice(offset, offset + limit), rows.length, offset),
      );
    vi.spyOn(knowledgeSdk, "deleteDocument").mockImplementation(async () => {
      rows = many.slice(0, 50);
    });

    renderDetail();
    await waitFor(() => expect(screen.getByText("doc-0.pdf")).toBeInTheDocument());
    await userEvent.click(within(screen.getByTestId("doc-table-pagination")).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("doc-50.pdf")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId(`doc-delete-${many[50].id}`));
    await confirmPopover("Delete");

    await waitFor(() => expect(screen.getByText("doc-0.pdf")).toBeInTheDocument());
    expect(listSpy).toHaveBeenLastCalledWith("support-docs", { limit: 50, offset: 0 }, undefined);
  }, FULL_PAGE_TIMEOUT_MS);
});

// ---------------------------------------------------------------------------
// B-175 L3 — pending slot: "previous version still in use" + discard.
// ---------------------------------------------------------------------------

const PENDING_FAILED: knowledgeSdk.KnowledgeDocument = {
  ...DOCS[0],
  status: "failed",
  error: "parse failed",
  has_pending: true,
};

describe("DocumentsTab — pending re-upload (B-175 L3)", () => {
  it("a failed re-upload says the previous version is still in use", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PENDING_FAILED]));

    renderDetail();
    const marker = await screen.findByTestId(`doc-pending-${PENDING_FAILED.id}`);
    expect(marker).toHaveTextContent("Previous version still in use");
    expect(screen.getByTestId(`doc-discard-pending-${PENDING_FAILED.id}`)).toBeEnabled();
  });

  it("no marker and no discard button without a pending upload", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage(DOCS));

    renderDetail();
    await waitFor(() => expect(screen.getByText("faq.pdf")).toBeInTheDocument());
    expect(screen.queryByTestId(`doc-pending-${DOCS[0].id}`)).not.toBeInTheDocument();
    expect(screen.queryByTestId(`doc-discard-pending-${DOCS[0].id}`)).not.toBeInTheDocument();
  });

  it("with no chunks serving the marker says nothing is in use yet", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(
      docPage([{ ...PENDING_FAILED, chunk_count: 0 }]),
    );

    renderDetail();
    const marker = await screen.findByTestId(`doc-pending-${PENDING_FAILED.id}`);
    expect(marker).toHaveTextContent("New version pending, nothing in use yet");
  });

  it("discard calls the SDK after confirming and refreshes the table", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([PENDING_FAILED]))
      .mockResolvedValue(docPage(DOCS));
    const discardSpy = vi
      .spyOn(knowledgeSdk, "discardPendingDocument")
      .mockResolvedValue(DOCS[0]);

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-discard-pending-${PENDING_FAILED.id}`));
    expect(discardSpy).not.toHaveBeenCalled(); // behind the confirm
    await confirmPopover("Discard new version");

    await waitFor(() =>
      expect(discardSpy).toHaveBeenCalledWith("support-docs", PENDING_FAILED.id),
    );
    await waitFor(() =>
      expect(screen.queryByTestId(`doc-pending-${PENDING_FAILED.id}`)).not.toBeInTheDocument(),
    );
    expect(listSpy).toHaveBeenCalledTimes(2);
  });

  it("a 409 (nothing pending any more) still refreshes the table", async () => {
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([PENDING_FAILED]))
      .mockResolvedValue(docPage(DOCS));
    vi.spyOn(knowledgeSdk, "discardPendingDocument").mockRejectedValue(
      new ApiError("document has no pending upload", "HTTP_409", 409),
    );

    renderDetail();
    await userEvent.click(await screen.findByTestId(`doc-discard-pending-${PENDING_FAILED.id}`));
    await confirmPopover("Discard new version");

    expect(await screen.findByText(/has no pending new version/)).toBeInTheDocument();
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
  });

  it("readonly (switched tenant): discard is disabled", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(knowledgeSdk, "getBase").mockResolvedValue(BASE);
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PENDING_FAILED]));

    renderDetail();
    expect(
      await screen.findByTestId(`doc-discard-pending-${PENDING_FAILED.id}`),
    ).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// B-175 L3 — server paging edge cases on the tab itself.
// ---------------------------------------------------------------------------

function sixtyDocs(prefix: string): knowledgeSdk.KnowledgeDocument[] {
  return Array.from({ length: 60 }, (_, i) => ({
    ...DOCS[0],
    id: `55555555-5555-5555-5555-${String(i).padStart(12, "0")}`,
    filename: `${prefix}-${i}.pdf`,
  }));
}

function tabTree(baseName: string) {
  return (
    <App>
      <DocumentsTab baseName={baseName} readScope={undefined} readonly={false} />
    </App>
  );
}

describe("DocumentsTab — server paging edges (B-175 L3)", () => {
  it("switching to another base starts again on page 1", async () => {
    const rows: Record<string, knowledgeSdk.KnowledgeDocument[]> = {
      alpha: sixtyDocs("alpha"),
      beta: sixtyDocs("beta"),
    };
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockImplementation(async (base, { limit, offset }) =>
        docPage(rows[base].slice(offset, offset + limit), rows[base].length, offset),
      );

    const { rerender } = render(tabTree("alpha"));
    await waitFor(() => expect(screen.getByText("alpha-0.pdf")).toBeInTheDocument());
    await userEvent.click(within(screen.getByTestId("doc-table-pagination")).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("alpha-59.pdf")).toBeInTheDocument());

    rerender(tabTree("beta"));
    await waitFor(() => expect(screen.getByText("beta-0.pdf")).toBeInTheDocument());
    expect(listSpy).not.toHaveBeenCalledWith("beta", { limit: 50, offset: 50 }, undefined);
  }, FULL_PAGE_TIMEOUT_MS);

  it("a slow response for the previous page never overwrites the current page", async () => {
    const many = sixtyDocs("doc");
    let releasePage1!: (page: knowledgeSdk.DocumentsPage) => void;
    let first = true;
    vi.spyOn(knowledgeSdk, "listDocuments").mockImplementation((_base, { limit, offset }) => {
      const page = docPage(many.slice(offset, offset + limit), many.length, offset);
      if (offset === 0 && !first) {
        // the second page-1 read hangs until released
        return new Promise((resolve) => {
          releasePage1 = resolve;
        });
      }
      first = false;
      return Promise.resolve(page);
    });

    render(tabTree("alpha"));
    await waitFor(() => expect(screen.getByText("doc-0.pdf")).toBeInTheDocument());
    const pager = screen.getByTestId("doc-table-pagination");
    await userEvent.click(within(pager).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("doc-59.pdf")).toBeInTheDocument());
    // fireEvent: the loading mask sets pointer-events:none, which userEvent honours.
    fireEvent.click(within(pager).getByTitle("1")); // hangs
    fireEvent.click(within(pager).getByTitle("2"));
    await waitFor(() => expect(screen.getByText("doc-59.pdf")).toBeInTheDocument());

    await act(async () => {
      releasePage1(docPage(many.slice(0, 50), many.length, 0));
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(screen.queryByText("doc-0.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("doc-59.pdf")).toBeInTheDocument();
  }, FULL_PAGE_TIMEOUT_MS);
});
