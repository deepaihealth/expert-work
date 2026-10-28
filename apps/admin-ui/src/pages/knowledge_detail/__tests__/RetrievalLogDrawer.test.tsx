/**
 * One retrieval record — B-175 P2-PR4.
 *
 * The record keeps ids and scores only; the drawer shows each result's
 * document (or that it was deleted), the four scores, and the passage text
 * read live (or that it is gone). An agent's record links to its
 * conversation (anyone who can read records can open it) and, for admins,
 * its run (run detail is owner-or-admin). Those pages read the ambient tenant
 * scope, so the links are withheld when this base is read under another
 * tenant's scope than the ambient one. Data: the shared contract fixture.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import "../../../i18n";

import LOGS_FIXTURE from "../../../api/__fixtures__/knowledge_retrieval_logs.json";
import { ApiError } from "../../../api/client";
import * as logsSdk from "../../../api/knowledge_logs";
import { RetrievalLogDrawer } from "../RetrievalLogDrawer";

const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const HOME_TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const FOREIGN_TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

const identityRef = vi.hoisted(() => ({
  current: { roles: ["admin"] as string[], isSystemAdmin: false },
}));
vi.mock("../../../auth/AuthContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../../auth/AuthContext")>();
  return {
    ...original,
    useAuth: () => ({
      status: "authenticated" as const,
      identity: {
        kind: "jwt" as const,
        subject: "u1",
        subjectType: "user" as const,
        homeTenantId: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        roles: identityRef.current.roles,
        isSystemAdmin: identityRef.current.isSystemAdmin,
        homeIsPlatform: false,
        displayName: "u1",
        serverResolved: true,
      },
      token: "t",
      login: () => {},
      logout: () => {},
      refreshIdentity: async () => {},
    }),
  };
});

const DETAIL = LOGS_FIXTURE.detail as unknown as logsSdk.RetrievalLogDetail;
const PASSAGES = LOGS_FIXTURE.passages.passages as unknown as logsSdk.LivePassage[];
const THREAD = DETAIL.thread_id!;
const RUN = DETAIL.run_id!;

function ConversationPage() {
  const location = useLocation();
  const state = location.state as { from?: string; fromLabel?: string } | null;
  return (
    <div data-testid="conversation-page">
      {state?.from} | {state?.fromLabel}
    </div>
  );
}

function mount(
  detail: Partial<logsSdk.RetrievalLogDetail> = {},
  { readScope, passages = PASSAGES }: { readScope?: string; passages?: logsSdk.LivePassage[] } = {},
) {
  const getLog = vi
    .spyOn(logsSdk, "getRetrievalLog")
    .mockResolvedValue({ ...DETAIL, ...detail });
  const getPassages = vi.spyOn(logsSdk, "getRetrievalLogPassages").mockResolvedValue(passages);
  render(
    <App>
      <MemoryRouter initialEntries={["/knowledge/support-docs/logs"]}>
        <Routes>
          <Route
            path="/knowledge/:name/:tab"
            element={
              <RetrievalLogDrawer
                baseName="support-docs"
                logId={DETAIL.id}
                readScope={readScope}
                onClose={() => {}}
              />
            }
          />
          <Route path="/conversations/:threadId" element={<ConversationPage />} />
        </Routes>
      </MemoryRouter>
    </App>,
  );
  return { getLog, getPassages };
}

beforeEach(() => {
  scopeRef.current = undefined;
  identityRef.current = { roles: ["admin"], isSystemAdmin: false };
});
afterEach(() => vi.restoreAllMocks());

describe("RetrievalLogDrawer — the record", () => {
  it("reads the record and its live text under the page's scope", async () => {
    const { getLog, getPassages } = mount({}, { readScope: FOREIGN_TENANT });
    await screen.findByTestId("kb-log-query");
    expect(getLog).toHaveBeenCalledWith("support-docs", DETAIL.id, FOREIGN_TENANT);
    expect(getPassages).toHaveBeenCalledWith("support-docs", DETAIL.id, FOREIGN_TENANT);
    expect(screen.getByTestId("kb-log-query")).toHaveTextContent("What is the deductible?");
  });

  it("shows the parameters, base defaults named as such", async () => {
    mount();
    const params = await screen.findByTestId("kb-log-params");
    expect(params).toHaveTextContent("Results (top-k)5");
    expect(params).toHaveTextContent("Retrieval methodbase default");
  });

  it("says how the search ran", async () => {
    mount({
      flags: { ...DETAIL.flags, embedding_degraded: true, unsearched_bases: ["other-kb"] },
    });
    const flags = await screen.findByTestId("kb-log-flags");
    expect(flags).toHaveTextContent("Candidates: 50");
    expect(flags).toHaveTextContent("Rerank model: vendor/rerank-model");
    expect(screen.getByTestId("kb-log-embedding-degraded")).toBeInTheDocument();
    expect(screen.getByTestId("kb-log-unsearched")).toHaveTextContent("other-kb");
    expect(screen.getByTestId("kb-log-duplicates-dropped")).toHaveTextContent(
      "1 duplicate passage was left out.",
    );
  });

  it("names the exception of a search that failed", async () => {
    mount({ status: "error", result_count: 0, results: [], flags: { error: "TimeoutError" } });
    expect(await screen.findByTestId("kb-log-error")).toHaveTextContent("TimeoutError");
  });

  it("says when the search covered several bases", async () => {
    mount({ kb_ids: [...DETAIL.kb_ids, "another-base-id"] });
    expect(await screen.findByTestId("kb-log-multibase")).toHaveTextContent("covered 2 bases");
  });

  it("says a record that is gone is gone", async () => {
    vi.spyOn(logsSdk, "getRetrievalLog").mockRejectedValue(
      new ApiError("retrieval log not found", "NOT_FOUND", 404),
    );
    vi.spyOn(logsSdk, "getRetrievalLogPassages").mockResolvedValue([]);
    render(
      <App>
        <MemoryRouter>
          <RetrievalLogDrawer baseName="support-docs" logId="gone" readScope={undefined} onClose={() => {}} />
        </MemoryRouter>
      </App>,
    );
    expect(await screen.findByTestId("kb-log-missing")).toHaveTextContent(
      "passed the retention period",
    );
  });

  it("shows any other failure as an error", async () => {
    vi.spyOn(logsSdk, "getRetrievalLog").mockRejectedValue(
      new ApiError("boom", "INTERNAL", 500),
    );
    vi.spyOn(logsSdk, "getRetrievalLogPassages").mockResolvedValue([]);
    render(
      <App>
        <MemoryRouter>
          <RetrievalLogDrawer baseName="support-docs" logId="x" readScope={undefined} onClose={() => {}} />
        </MemoryRouter>
      </App>,
    );
    expect(await screen.findByTestId("kb-log-failed")).toHaveTextContent("INTERNAL: boom");
  });
});

describe("RetrievalLogDrawer — results", () => {
  it("shows each result's document, passage, scores and live text with the hit marked", async () => {
    mount();
    const first = await screen.findByTestId("kb-log-result-0");
    expect(within(first).getByTestId("kb-log-result-doc-0")).toHaveTextContent("D12 · Guide");
    expect(first).toHaveTextContent("Passage #1 · chunk #3");
    expect(within(first).getByTestId("kb-log-result-scores-0")).toHaveTextContent("Rerank 0.9112");
    const text = await within(first).findByTestId("kb-log-result-text-0");
    const mark = within(text).getByTestId("kb-log-result-mark-0");
    expect(mark.textContent).toBe("The deductible is 500 per year.");
    expect(text).toHaveTextContent("Sample paragraph one.");
    expect(screen.getByTestId("kb-log-text-live-note")).toBeInTheDocument();
  });

  it("says a deleted document is deleted and shows no text for it", async () => {
    mount();
    const second = await screen.findByTestId("kb-log-result-1");
    expect(within(second).getByTestId("kb-log-result-doc-1-deleted")).toHaveTextContent(
      "Document deleted",
    );
    expect(second).toHaveTextContent("Chunk #0");
    await screen.findByTestId("kb-log-result-text-0");
    expect(screen.queryByTestId("kb-log-result-text-1")).toBeNull();
    expect(screen.queryByTestId("kb-log-result-text-gone-1")).toBeNull();
  });

  it("says a passage that is gone is gone", async () => {
    mount({}, { passages: [{ passage: null, hit: null }, { passage: null, hit: null }] });
    expect(await screen.findByTestId("kb-log-result-text-gone-0")).toHaveTextContent(
      "no longer exists",
    );
  });

  it("shows the passage without a mark when the matched chunk changed, and a switch-off", async () => {
    const [first] = PASSAGES;
    mount(
      {},
      {
        passages: [
          { passage: { ...first.passage!, enabled: false }, hit: null },
          { passage: null, hit: null },
        ],
      },
    );
    expect(await screen.findByTestId("kb-log-result-hit-gone-0")).toBeInTheDocument();
    expect(screen.getByTestId("kb-log-result-text-0")).toHaveTextContent("Sample paragraph one.");
    expect(screen.queryByTestId("kb-log-result-mark-0")).toBeNull();
    expect(screen.getByTestId("kb-log-result-disabled-0")).toHaveTextContent("Switched off");
  });

  it("still shows the record when the text cannot be read", async () => {
    vi.spyOn(logsSdk, "getRetrievalLog").mockResolvedValue(DETAIL);
    vi.spyOn(logsSdk, "getRetrievalLogPassages").mockRejectedValue(
      new ApiError("principal lacks required role", "FORBIDDEN", 403),
    );
    render(
      <App>
        <MemoryRouter>
          <RetrievalLogDrawer baseName="support-docs" logId={DETAIL.id} readScope={undefined} onClose={() => {}} />
        </MemoryRouter>
      </App>,
    );
    expect(await screen.findByTestId("kb-log-text-failed")).toHaveTextContent("FORBIDDEN");
    expect(screen.getByTestId("kb-log-result-scores-0")).toBeInTheDocument();
  });
});

describe("RetrievalLogDrawer — conversation and run", () => {
  it("links an agent's record to its conversation, back to this tab", async () => {
    mount();
    const link = await screen.findByTestId("kb-log-conversation-link");
    expect(link).toHaveAttribute("href", `/conversations/${THREAD}`);
    await userEvent.click(link);
    expect(await screen.findByTestId("conversation-page")).toHaveTextContent(
      "/knowledge/support-docs/logs | support-docs",
    );
  });

  it("links an admin to the run", async () => {
    mount();
    expect(await screen.findByTestId("kb-log-run-link")).toHaveAttribute(
      "href",
      `/runs/${THREAD}/${RUN}`,
    );
  });

  it("gives a viewer the conversation but not the run (run detail is owner-or-admin)", async () => {
    identityRef.current = { roles: ["viewer"], isSystemAdmin: false };
    mount();
    await screen.findByTestId("kb-log-conversation-link");
    expect(screen.queryByTestId("kb-log-run-link")).toBeNull();
  });

  it("withholds the links when the base is read under another tenant than the ambient scope", async () => {
    scopeRef.current = "*";
    mount({}, { readScope: FOREIGN_TENANT });
    expect(await screen.findByTestId("kb-log-links-other-tenant")).toHaveTextContent(
      "Switch to this tenant",
    );
    expect(screen.queryByTestId("kb-log-conversation-link")).toBeNull();
    expect(screen.queryByTestId("kb-log-run-link")).toBeNull();
  });

  it("links when switched into the tenant the base is read under", async () => {
    scopeRef.current = FOREIGN_TENANT;
    mount({}, { readScope: FOREIGN_TENANT });
    expect(await screen.findByTestId("kb-log-conversation-link")).toBeInTheDocument();
  });

  it("links on the home tenant's base in the all-tenants view", async () => {
    scopeRef.current = "*";
    mount({}, { readScope: undefined });
    expect(await screen.findByTestId("kb-log-conversation-link")).toBeInTheDocument();
  });

  it("treats the home tenant's own id as the home scope", async () => {
    mount({}, { readScope: HOME_TENANT });
    expect(await screen.findByTestId("kb-log-conversation-link")).toBeInTheDocument();
  });

  it("names the employee behind a console test, with no conversation", async () => {
    mount({ source: "console_test", run_id: null, thread_id: null, actor_id: "emp-7" });
    expect(await screen.findByTestId("kb-log-actor")).toHaveTextContent("emp-7");
    expect(screen.queryByTestId("kb-log-conversation-link")).toBeNull();
    expect(screen.queryByTestId("kb-log-links-other-tenant")).toBeNull();
  });
});
