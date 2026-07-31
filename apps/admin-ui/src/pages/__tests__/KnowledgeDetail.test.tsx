/**
 * KnowledgeDetail tests — KB commercial uplift.
 *
 * Stubs the knowledge SDK. Covers the detail shell (stats + needs-reindex
 * banner/reindex), the documents tab (localized status + re-ingest), the
 * retrieval-test tab (run → scored results), and the settings tab (PATCH).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import { KnowledgeDetail } from "../KnowledgeDetail";

// Cross-tenant W3 — the page reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
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
});
