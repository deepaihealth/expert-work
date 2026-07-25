/**
 * ConversationDetail tests — the conversation-centric operations view.
 *
 * ``getConversation`` is stubbed; the page renders under a routed
 * MemoryRouter so ``useParams`` resolves ``:threadId``. Asserts the
 * summary rollup + the run list drill-down target, plus the read-only
 * debug-console transcript (轮次卡时间线) that replaces the flat message
 * block whenever ``/messages`` pairs 1:1 with the thread's runs.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from "react-router-dom";
import "../../i18n";

import { setStoredToken } from "../../api/client";
import * as convoSdk from "../../api/conversations";
import * as runsSdk from "../../api/runs";
import * as sessionsSdk from "../../api/sessions";
import type { SseEvent } from "../../api/sessions";
import { AuthProvider } from "../../auth/AuthContext";
import { ConversationDetail } from "../ConversationDetail";
import type { ConversationDetail as ConversationDetailModel } from "../../api/conversations";

const THREAD_ID = "44444444-4444-4444-4444-444444444444";
const TENANT_ID = "22222222-2222-2222-2222-222222222222";
const RUN_1 = "33333333-3333-3333-3333-333333333333";
const RUN_2 = "33333333-3333-3333-3333-333333333334";

function jwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

// jsdom has no IntersectionObserver — stub one that treats every observed
// element as immediately visible (fires its callback synchronously from
// ``observe``), mirroring ``PlaygroundTab.test.tsx``: a history row's lazy
// replay kicks off without simulating real scrolling.
class IOStub {
  private cb: IntersectionObserverCallback;
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
  }
  observe = (el: Element) => {
    this.cb(
      [{ isIntersecting: true, target: el } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  };
  unobserve = () => {};
  disconnect = () => {};
  takeRecords = () => [];
  root = null;
  rootMargin = "";
  thresholds: number[] = [];
}

function makeStream(events: SseEvent[]): AsyncGenerator<SseEvent, void, void> {
  return (async function* () {
    for (const e of events) yield e;
  })();
}

/** A replayed run: run metadata → an agent step calling ``search`` → the
 *  tool result → the final answer → terminal frame. */
const replayEvents: SseEvent[] = [
  { id: "1", event: "metadata", data: { run_id: RUN_1 }, rawData: "", receivedAt: "" },
  {
    id: "2",
    event: "updates",
    data: {
      agent: {
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [
              { id: "c1", name: "search", args: { q: "expert-work" }, type: "tool_call" },
            ],
          },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "3",
    event: "updates",
    data: {
      tools: {
        messages: [
          { type: "tool", tool_call_id: "c1", name: "search", content: "3 hits", status: "success" },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "4",
    event: "updates",
    data: { agent: { messages: [{ type: "ai", content: "replayed answer" }] } },
    rawData: "",
    receivedAt: "",
  },
  { id: "5", event: "end", data: {}, rawData: "", receivedAt: "" },
];

const TWO_TURNS: sessionsSdk.HistoryMessage[] = [
  { role: "user", content: "I was charged twice" },
  { role: "assistant", content: "Refund case opened" },
  { role: "user", content: "when is it refunded?" },
  { role: "assistant", content: "In 3 days" },
];

const TWO_RUNS = [
  { runId: RUN_1, status: "success" as const, isResume: false, createdAt: "2026-06-30T12:00:00Z" },
  { runId: RUN_2, status: "success" as const, isResume: true, createdAt: "2026-06-30T12:05:00Z" },
];

const CONVO: ConversationDetailModel = {
  thread_id: THREAD_ID,
  tenant_id: "22222222-2222-2222-2222-222222222222",
  user_id: "88888888-8888-8888-8888-888888888888",
  agent_name: "code-reviewer",
  agent_version: "1.0.0",
  title: "refund question",
  status: "active",
  created_at: "2026-06-30T12:00:00Z",
  updated_at: "2026-06-30T12:05:00Z",
  run_count: 2,
  error_count: 1,
  pending_count: 0,
  last_run_at: "2026-06-30T12:05:00Z",
  tokens: {
    input_tokens: 150,
    output_tokens: 30,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    total_tokens: 180,
    llm_calls: 2,
    models: ["claude-sonnet-4-5"],
  },
  runs: [
    {
      run_id: "33333333-3333-3333-3333-333333333333",
      thread_id: THREAD_ID,
      user_id: "88888888-8888-8888-8888-888888888888",
      status: "success",
      is_resume: false,
      error: null,
      created_at: "2026-06-30T12:00:00Z",
      updated_at: "2026-06-30T12:01:00Z",
      finished_at: "2026-06-30T12:01:00Z",
      trace_id: "tr-1",
      tokens: null,
    },
    {
      run_id: "33333333-3333-3333-3333-333333333334",
      thread_id: THREAD_ID,
      user_id: "88888888-8888-8888-8888-888888888888",
      status: "error",
      is_resume: false,
      error: "boom",
      created_at: "2026-06-30T12:05:00Z",
      updated_at: "2026-06-30T12:05:30Z",
      finished_at: "2026-06-30T12:05:30Z",
      trace_id: "tr-2",
      tokens: null,
    },
  ],
};

/** ``AuthProvider`` backs the tenant guard + the system_admin gate;
 *  antd ``App`` is what ``ToolCallCard`` reaches for via ``App.useApp()``
 *  (the real tree wraps the router in ``<AntApp>`` — see ``App.tsx``). */
function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/conversations/${THREAD_ID}`]}>
      <AuthProvider>
        <App>
          <Routes>
            <Route path="/conversations/:threadId" element={<ConversationDetail />} />
          </Routes>
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

// jsdom implements neither half of the object-URL API that ``downloadJson``
// drives; stub both and restore after each test.
const realCreateObjectURL = URL.createObjectURL;
const realRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:conversation-export");
  URL.revokeObjectURL = vi.fn();
  // Same tenant as ``CONVO`` — the transcript rebuild is tenant-scoped (D3).
  setStoredToken(jwt({ sub: "u", tenant_id: TENANT_ID, roles: ["admin"] }));
  vi.stubGlobal("IntersectionObserver", IOStub);
  // Default: zero runs → ``buildHistoryTurns``' count guard rejects the
  // pairing, so a test that doesn't opt in keeps today's flat transcript.
  vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([]);
  vi.spyOn(runsSdk, "streamRunEvents").mockImplementation(() => makeStream([]));
});

afterEach(() => {
  URL.createObjectURL = realCreateObjectURL;
  URL.revokeObjectURL = realRevokeObjectURL;
  setStoredToken(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ConversationDetail", () => {
  it("renders the conversation summary + its run list", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-root")).toBeInTheDocument(),
    );
    // Summary token rollup.
    expect(screen.getByTestId("conversation-tokens")).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-4-5")).toBeInTheDocument();
    // Run list with the failed run's error surfaced.
    expect(screen.getByTestId("conversation-runs-table")).toBeInTheDocument();
    expect(
      screen.getByTestId("conversation-run-error-33333333-3333-3333-3333-333333333334"),
    ).toBeInTheDocument();
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(convoSdk, "getConversation").mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-error")).toBeInTheDocument(),
    );
  });

  it("renders the message transcript (M1.5) when the endpoint returns turns", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
    const messagesSpy = vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([
      { role: "user", content: "I was charged twice" },
      { role: "assistant", content: "Refund case opened" },
    ]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("conversation-messages")).toBeInTheDocument(),
    );
    expect(screen.getByText("I was charged twice")).toBeInTheDocument();
    expect(screen.getByText("Refund case opened")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-message-0")).toBeInTheDocument();
    // The thread's tenant rides along — a system_admin's cross-tenant
    // drill-in reads the right tenant's checkpoint.
    expect(messagesSpy).toHaveBeenCalledWith(THREAD_ID, CONVO.tenant_id);
  });

  it("hides the transcript panel when the messages endpoint fails", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
    vi.spyOn(sessionsSdk, "getSessionMessages").mockRejectedValue(new Error("403"));

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-root")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("conversation-messages")).not.toBeInTheDocument();
  });

  it("shows an explicit empty state for a conversation with no messages", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
    vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("conversation-messages")).toBeInTheDocument(),
    );
    expect(screen.getByText("No messages recorded for this conversation.")).toBeInTheDocument();
  });

  it("back link restores the browser view carried in location.state.from", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: `/conversations/${THREAD_ID}`,
            state: { from: "/conversations?errors=1&window=24&page=2" },
          },
        ]}
      >
        <AuthProvider>
          <App>
            <Routes>
              <Route path="/conversations/:threadId" element={<ConversationDetail />} />
            </Routes>
          </App>
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-root")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("page-header-back")).toHaveAttribute(
      "href",
      "/conversations?errors=1&window=24&page=2",
    );
  });

  it("back link falls back to the agent conversations tab without state.from", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-root")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("page-header-back")).toHaveAttribute(
      "href",
      `/agents/${CONVO.agent_name}/${CONVO.agent_version}/conversations`,
    );
  });

  // 「对话详情页 = 只读调试台」 — the flat message block becomes the debug
  // console's own read-only turn cards whenever the transcript pairs 1:1 with
  // the thread's runs; every failure mode degrades back to the flat block.
  describe("read-only turn timeline", () => {
    it("rebuilds the transcript as read-only turn cards when messages pair with runs", async () => {
      vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
      vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue(TWO_TURNS);
      vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue(TWO_RUNS);

      renderPage();

      await waitFor(() =>
        expect(screen.getAllByTestId("playground-turn")).toHaveLength(2),
      );
      // Each card carries its own user input + the stored answer as fallback.
      expect(screen.getByText("I was charged twice")).toBeInTheDocument();
      expect(screen.getByText("Refund case opened")).toBeInTheDocument();
      // …and the flat bubble rendering is gone.
      expect(screen.queryByTestId("conversation-message-0")).not.toBeInTheDocument();
    });

    it("replays a run when its row scrolls into view and renders the tool call", async () => {
      vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
      vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([
        { role: "user", content: "I was charged twice" },
        { role: "assistant", content: "Refund case opened" },
      ]);
      vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);
      const streamSpy = vi
        .spyOn(runsSdk, "streamRunEvents")
        .mockImplementation(() => makeStream(replayEvents));

      renderPage();

      await waitFor(() =>
        expect(streamSpy).toHaveBeenCalledWith(THREAD_ID, RUN_1, expect.anything()),
      );
      // The replayed content renders — not just the stored fallback text.
      expect(await screen.findByText("replayed answer")).toBeInTheDocument();
      expect(screen.getByTestId("step-timeline")).toBeInTheDocument();
      expect(screen.getByTestId("playground-tool-count")).toHaveTextContent("1");
      fireEvent.click(screen.getAllByTestId("step-head")[0]);
      const toolCards = screen.getAllByTestId("tool-call-card");
      expect(toolCards).toHaveLength(1);
      expect(within(toolCards[0]).getByText("search")).toBeInTheDocument();
    });

    it("exports a replayed turn's event stream for real (the button is not a no-op)", async () => {
      vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
      vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([
        { role: "user", content: "I was charged twice" },
        { role: "assistant", content: "Refund case opened" },
      ]);
      vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);
      vi.spyOn(runsSdk, "streamRunEvents").mockImplementation(() =>
        makeStream(replayEvents),
      );
      const downloaded: string[] = [];
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
        function (this: HTMLAnchorElement) {
          downloaded.push(this.download);
        },
      );
      // jsdom's Blob has no ``text()``, so record the parts on construction.
      const blobParts: BlobPart[][] = [];
      class RecordingBlob extends Blob {
        constructor(parts: BlobPart[], options?: BlobPropertyBag) {
          super(parts, options);
          blobParts.push(parts);
        }
      }
      vi.stubGlobal("Blob", RecordingBlob);

      renderPage();

      fireEvent.click(await screen.findByTestId("playground-export-json"));

      await waitFor(() => expect(downloaded).toEqual([`expert-work-events-${RUN_1}.json`]));
      const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob;
      expect(blob.type).toBe("application/json");
      const payload = JSON.parse(blobParts[0][0] as string);
      expect(payload.run_id).toBe(RUN_1);
      expect(payload.thread_id).toBe(THREAD_ID);
      expect(payload.source).toBe("backend");
      expect(payload.events).toHaveLength(replayEvents.length);
    });

    it("keeps the flat message block when the message/run counts don't line up", async () => {
      vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
      vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue(TWO_TURNS);
      // 2 user turns vs 1 run — buildHistoryTurns' count guard rejects it.
      vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);

      renderPage();

      await waitFor(() =>
        expect(screen.getByTestId("conversation-message-0")).toBeInTheDocument(),
      );
      expect(screen.getByText("I was charged twice")).toBeInTheDocument();
      expect(screen.queryByTestId("playground-turn")).not.toBeInTheDocument();
    });

    it("stays flat and issues no replay for a cross-tenant thread", async () => {
      // system_admin drilling into another tenant's conversation: the replay /
      // runs endpoints take no ``tenant_id``, so rebuilding could only 404 (D3).
      setStoredToken(
        jwt({
          sub: "u",
          tenant_id: "99999999-9999-9999-9999-999999999999",
          roles: ["system_admin"],
        }),
      );
      vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
      vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue(TWO_TURNS);
      // Would pair 1:1 if it were ever asked — the guard is what keeps it flat.
      const runsSpy = vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue(TWO_RUNS);
      const streamSpy = vi
        .spyOn(runsSdk, "streamRunEvents")
        .mockImplementation(() => makeStream(replayEvents));

      renderPage();

      await waitFor(() =>
        expect(screen.getByTestId("conversation-message-0")).toBeInTheDocument(),
      );
      expect(screen.queryByTestId("playground-turn")).not.toBeInTheDocument();
      expect(runsSpy).not.toHaveBeenCalled();
      expect(streamSpy).not.toHaveBeenCalled();
    });
  });

  it("every run row carries an explicit drill-in link to its run detail, and clicking it navigates exactly once (I-1: one 'back' returns to the conversation page)", async () => {
    vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);

    // The link sits inside a row that is *also* clickable (``onRow.onClick``
    // navigates to the same run). A ``MemoryRouter``+``Routes`` render can't
    // observe the push *count* — only a data router's ``router.state``/
    // ``router.navigate(-1)`` can distinguish "navigated once" from
    // "navigated twice to the same URL" (I-1).
    const router = createMemoryRouter(
      [
        { path: "/conversations/:threadId", element: <ConversationDetail /> },
        { path: "/runs/:threadId/:runId", element: <div data-testid="run-detail-stub" /> },
      ],
      { initialEntries: [`/conversations/${THREAD_ID}`] },
    );
    render(
      <AuthProvider>
        <App>
          <RouterProvider router={router} />
        </App>
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("conversation-runs-table")).toBeInTheDocument(),
    );
    expect(screen.getByTestId(`conversation-run-open-${RUN_1}`)).toHaveAttribute(
      "href",
      `/runs/${THREAD_ID}/${RUN_1}`,
    );
    expect(screen.getByTestId(`conversation-run-open-${RUN_2}`)).toHaveAttribute(
      "href",
      `/runs/${THREAD_ID}/${RUN_2}`,
    );

    fireEvent.click(screen.getByTestId(`conversation-run-open-${RUN_1}`));

    await waitFor(() =>
      expect(screen.getByTestId("run-detail-stub")).toBeInTheDocument(),
    );
    expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}/${RUN_1}`);

    router.navigate(-1);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/conversations/${THREAD_ID}`),
    );
    // Going back remounts ``ConversationDetail`` (it re-fetches), so it's
    // briefly back on the loading Skeleton before the root testid reappears.
    await waitFor(() =>
      expect(screen.getByTestId("conversation-detail-root")).toBeInTheDocument(),
    );
  });
});
