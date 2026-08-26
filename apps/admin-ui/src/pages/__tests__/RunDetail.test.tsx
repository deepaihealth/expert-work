/**
 * RunDetail tests — 调试台重设计 PR-B Task 4 (this page's first page-level
 * suite; the old ``PlanPanel``/``TraceToolbar``/``EventStreamPanel`` trio had
 * its own component tests, retired alongside the page rewrite).
 *
 * Mocking mirrors ``ConversationDetail.test.tsx``: ``getRun`` + a stubbed
 * ``TenantScopeContext`` for the tenant-scope threading assertion, plus
 * ``useHistoryTurns``' own dependencies (``getSessionMessages`` /
 * ``listThreadRuns`` / ``streamRunEvents``) so the single-run trajectory
 * pipeline runs for real end to end (pairing → replay → ``TrajectoryView``).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import type { NavigateFunction } from "react-router-dom";
import "../../i18n";

import { setStoredToken } from "../../api/client";
import * as convoSdk from "../../api/conversations";
import * as planSdk from "../../api/plan";
import * as runsSdk from "../../api/runs";
import type { RunDetail as RunDetailModel } from "../../api/runs";
import * as sessionsSdk from "../../api/sessions";
import type { SseEvent } from "../../api/sessions";
import { AuthProvider } from "../../auth/AuthContext";
import { RunDetail } from "../RunDetail";

const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const THREAD_ID = "55555555-5555-5555-5555-555555555555";
const TENANT_ID = "22222222-2222-2222-2222-222222222222";
const RUN_1 = "44444444-4444-4444-4444-444444444444";
const RUN_2 = "44444444-4444-4444-4444-444444444445";

function jwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function makeStream(events: SseEvent[]): AsyncGenerator<SseEvent, void, void> {
  return (async function* () {
    for (const e of events) yield e;
  })();
}

function runDetail(overrides: Partial<RunDetailModel> = {}): RunDetailModel {
  return {
    run_id: RUN_1,
    thread_id: THREAD_ID,
    status: "success",
    pending_approval: null,
    trace_id: null,
    ...overrides,
  };
}

/** RUN_1: a replayed run — metadata → agent step calling ``search`` → the
 *  tool result → the final answer → terminal frame (same shape as
 *  ConversationDetail.test.tsx's ``replayEvents``). */
const RUN_1_EVENTS: SseEvent[] = [
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
    data: { agent: { messages: [{ type: "ai", content: "run one's answer" }] } },
    rawData: "",
    receivedAt: "",
  },
  { id: "5", event: "end", data: {}, rawData: "", receivedAt: "" },
];

const RUN_2_EVENTS: SseEvent[] = [
  { id: "1", event: "metadata", data: { run_id: RUN_2 }, rawData: "", receivedAt: "" },
  {
    id: "2",
    event: "updates",
    data: { agent: { messages: [{ type: "ai", content: "run two's answer" }] } },
    rawData: "",
    receivedAt: "",
  },
  { id: "3", event: "end", data: {}, rawData: "", receivedAt: "" },
];

const TWO_TURNS: sessionsSdk.HistoryMessage[] = [
  { role: "user", content: "first question" },
  { role: "assistant", content: "run one's answer" },
  { role: "user", content: "second question" },
  { role: "assistant", content: "run two's answer" },
];

const TWO_RUNS = [
  { runId: RUN_1, status: "success" as const, isResume: false, createdAt: "2026-06-30T12:00:00Z", finishedAt: null, error: null, tokens: null },
  { runId: RUN_2, status: "success" as const, isResume: true, createdAt: "2026-06-30T12:05:00Z", finishedAt: null, error: null, tokens: null },
];

const CONVO: convoSdk.ConversationDetail = {
  thread_id: THREAD_ID,
  tenant_id: TENANT_ID,
  user_id: "88888888-8888-8888-8888-888888888888",
  agent_name: "code-reviewer",
  agent_version: "1.0.0",
  title: "refund question",
  status: "active",
  created_at: "2026-06-30T12:00:00Z",
  updated_at: "2026-06-30T12:05:00Z",
  run_count: 2,
  error_count: 0,
  pending_count: 0,
  last_run_at: "2026-06-30T12:05:00Z",
  tokens: null,
  runs: [],
};

function renderPage(runId: string = RUN_1) {
  return render(
    <MemoryRouter initialEntries={[`/runs/${THREAD_ID}/${runId}`]}>
      <AuthProvider>
        <App>
          <Routes>
            <Route path="/runs/:threadId/:runId" element={<RunDetail />} />
          </Routes>
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setStoredToken(jwt({ sub: "u", tenant_id: TENANT_ID, roles: ["admin"] }));
  vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);
  vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([]);
  vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([]);
  vi.spyOn(runsSdk, "streamRunEvents").mockImplementation(() => makeStream([]));
  vi.spyOn(planSdk, "getThreadPlan").mockResolvedValue(null);
  vi.spyOn(planSdk, "updateThreadPlan");
});

afterEach(() => {
  setStoredToken(null);
  scopeRef.current = undefined;
  vi.restoreAllMocks();
});

describe("RunDetail", () => {
  // ① 渲染头部/元数据/RunSummaryPanel
  it("renders the page header, metadata card and run summary", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());

    renderPage();

    await waitFor(() => expect(screen.getByTestId("run-detail-root")).toBeInTheDocument());
    expect(screen.getByText(t_runId(RUN_1))).toBeInTheDocument();
    expect(screen.getByText(RUN_1)).toBeInTheDocument();
    expect(screen.getByText(THREAD_ID)).toBeInTheDocument();
  });

  // ② pending_approval 出 ApprovalCard
  it("renders the approval card when the run has a pending approval", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(
      runDetail({
        status: "paused",
        pending_approval: {
          request_id: "req-1",
          node: "deploy",
          reason_kind: "irreversible",
          action_summary: "Deploy build 42 to production",
          proposed_args: { target: "prod" },
          requested_at: "2026-06-10T08:00:00Z",
          timeout_at: "2026-06-11T08:00:00Z",
        },
      }),
    );

    renderPage();

    await waitFor(() => expect(screen.getByTestId("approval-card")).toBeInTheDocument());
    expect(screen.getByText("Deploy build 42 to production")).toBeInTheDocument();
  });

  it("does not render the approval card when there is no pending approval", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());
    renderPage();
    await waitFor(() => expect(screen.getByTestId("run-detail-root")).toBeInTheDocument());
    expect(screen.queryByTestId("approval-card")).not.toBeInTheDocument();
  });

  // ③ PlanCard 渲染且 running=run 非终态时锁编辑
  it("renders the plan card and locks editing while the run is live", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail({ status: "running" }));
    vi.spyOn(planSdk, "getThreadPlan").mockResolvedValue({
      goal: "ship the feature",
      steps: [{ id: "1", description: "write tests", status: "completed" }],
    });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("console-plan-card")).toBeInTheDocument());
    expect(screen.getByText("ship the feature")).toBeInTheDocument();
    expect(screen.getByTestId("plan-edit")).toBeDisabled();
  });

  it("leaves the plan card editable once the run reaches a terminal state", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail({ status: "success" }));
    vi.spyOn(planSdk, "getThreadPlan").mockResolvedValue({
      goal: "ship the feature",
      steps: [{ id: "1", description: "write tests", status: "completed" }],
    });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("console-plan-card")).toBeInTheDocument());
    expect(screen.getByTestId("plan-edit")).not.toBeDisabled();
  });

  // I-1 — a paused run (e.g. sitting at an approval gate) must not lock
  // plan editing: the backend's write guard (control_plane/api/plan.py's
  // ``_WRITE_BLOCKED_STATUSES = {PENDING, RUNNING}``) only blocks those two
  // statuses, and the pre-PR-B PlanPanel matched that; this page's PR-B
  // rewrite over-widened the lock to every ``ACTIVE_RUN_STATUSES`` member
  // (which includes ``paused``), a functional regression this test guards.
  it("leaves the plan card editable while the run is paused (I-1)", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(
      runDetail({
        status: "paused",
        pending_approval: {
          request_id: "req-1",
          node: "deploy",
          reason_kind: "irreversible",
          action_summary: "Deploy build 42 to production",
          proposed_args: { target: "prod" },
          requested_at: "2026-06-10T08:00:00Z",
          timeout_at: "2026-06-11T08:00:00Z",
        },
      }),
    );
    vi.spyOn(planSdk, "getThreadPlan").mockResolvedValue({
      goal: "ship the feature",
      steps: [{ id: "1", description: "write tests", status: "completed" }],
    });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("console-plan-card")).toBeInTheDocument());
    expect(screen.getByTestId("plan-edit")).not.toBeDisabled();
  });

  // Ruling 3 / I-2 — a terminal run's already-replayed plan frame is a
  // historical snapshot of what the plan looked like mid-run, not the
  // thread's current plan; the GET baseline (the thread's current plan) is
  // the one editable/savable object, so a terminal run's own event stream
  // must not let a stale plan frame override that baseline (or a Save
  // silently round-trip the stale snapshot instead of the real plan).
  it("shows the thread's current plan baseline, not a terminal run's stale replayed plan frame, and saves against it (Ruling 3 / I-2)", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail({ status: "success" }));
    vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([
      { role: "user", content: "first question" },
      { role: "assistant", content: "run one's answer" },
    ]);
    vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);
    vi.spyOn(runsSdk, "streamRunEvents").mockImplementation(() =>
      makeStream([
        ...RUN_1_EVENTS.slice(0, -1),
        {
          id: "plan-1",
          event: "plan",
          data: {
            goal: "stale plan v1",
            steps: [{ id: "1", description: "old step", status: "pending" }],
          },
          rawData: "",
          receivedAt: "",
        },
        RUN_1_EVENTS[RUN_1_EVENTS.length - 1],
      ]),
    );
    const currentPlan = {
      goal: "current plan v2",
      steps: [{ id: "1", description: "write tests", status: "completed" as const }],
    };
    vi.spyOn(planSdk, "getThreadPlan").mockResolvedValue(currentPlan);
    const putSpy = vi.spyOn(planSdk, "updateThreadPlan").mockResolvedValue(currentPlan);

    renderPage();

    await waitFor(() => expect(screen.getByTestId("console-plan-card")).toBeInTheDocument());
    expect(await screen.findByText("current plan v2")).toBeInTheDocument();
    expect(screen.queryByText("stale plan v1")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("plan-edit"));
    await userEvent.click(screen.getByTestId("plan-save"));
    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    expect(putSpy.mock.calls[0]?.[1]?.goal).toBe("current plan v2");
  });

  // ④ 轨迹区渲染该 run 的工具行
  it("renders this run's trajectory ledger with its tool call", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());
    vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([
      { role: "user", content: "first question" },
      { role: "assistant", content: "run one's answer" },
    ]);
    vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);
    const streamSpy = vi
      .spyOn(runsSdk, "streamRunEvents")
      .mockImplementation(() => makeStream(RUN_1_EVENTS));

    renderPage();

    await waitFor(() =>
      expect(streamSpy).toHaveBeenCalledWith(THREAD_ID, RUN_1, expect.anything()),
    );
    expect(await screen.findByTestId("console-traj-ledger")).toBeInTheDocument();
    expect(await screen.findByText(/search/)).toBeInTheDocument();
  });

  // ⑤ 单 run 过滤:两个 run 只出目标 run 的轮
  it("filters the trajectory to only this run's turn when the thread has two runs", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail({ run_id: RUN_1 }));
    vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue(TWO_TURNS);
    vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue(TWO_RUNS);
    const streamSpy = vi
      .spyOn(runsSdk, "streamRunEvents")
      .mockImplementation((_thread: string, runId: string) =>
        makeStream(runId === RUN_1 ? RUN_1_EVENTS : RUN_2_EVENTS),
      );

    renderPage(RUN_1);

    await waitFor(() => expect(screen.getByTestId("run-detail-root")).toBeInTheDocument());
    // Only RUN_1 is ever replayed — RUN_2 is part of the thread's paired
    // history but must not be fetched or shown on RUN_1's page.
    await waitFor(() => expect(streamSpy).toHaveBeenCalledWith(THREAD_ID, RUN_1, expect.anything()));
    expect(streamSpy).not.toHaveBeenCalledWith(THREAD_ID, RUN_2, expect.anything());
    expect(await screen.findByText("run one's answer")).toBeInTheDocument();
    expect(screen.queryByText("run two's answer")).not.toBeInTheDocument();
  });

  // ⑥ getRun/getConversation 都带 tenant scope
  it("threads the tenant scope through both getRun and getConversation", async () => {
    scopeRef.current = TENANT_ID;
    const runSpy = vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());
    const convoSpy = vi.spyOn(convoSdk, "getConversation").mockResolvedValue(CONVO);

    renderPage();

    await waitFor(() =>
      expect(runSpy).toHaveBeenCalledWith(THREAD_ID, RUN_1, TENANT_ID),
    );
    await waitFor(() => expect(convoSpy).toHaveBeenCalledWith(THREAD_ID, TENANT_ID));
  });

  // Degradation: a count-mismatch pairing failure shows an explicit empty
  // state instead of crashing or silently rendering nothing.
  it("shows the pairing-failed empty state when messages/runs don't line up", async () => {
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());
    vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue(TWO_TURNS);
    // 2 user turns vs 1 run — buildHistoryTurns' count guard rejects it.
    vi.spyOn(runsSdk, "listThreadRuns").mockResolvedValue([TWO_RUNS[0]]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("run-detail-pairing-failed")).toBeInTheDocument(),
    );
    // The copy itself renders inside the pinned container.
    expect(
      screen.getByText(
        "Couldn't pair this run's trajectory with the thread's messages — the two records don't line up.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("console-traj-ledger")).not.toBeInTheDocument();
  });

  // Ruling 4 (PR-B follow-up) — a same-page thread switch must never fire
  // the history load with the previous thread's tenant. The params flip a
  // render before the tenant state resets, so an untagged tenant fired
  // ``loadHistory(newThread, oldThreadsTenant)`` in that flip frame; the
  // state is now tagged with the thread it was resolved for. Today every
  // entry point remounts the page, so this drives the switch in-place via
  // ``useNavigate`` to reach the latent frame at all.
  it("never fires the history load with the previous thread's tenant on a same-page thread switch (Ruling 4)", async () => {
    const THREAD_B = "66666666-6666-6666-6666-666666666666";
    const RUN_B = "44444444-4444-4444-4444-444444444446";
    const TENANT_B = "33333333-3333-3333-3333-333333333333";
    vi.spyOn(runsSdk, "getRun").mockResolvedValue(runDetail());
    // Thread A's conversation settles immediately; thread B's stays pending
    // until the test resolves it — the whole race window.
    let resolveB: (c: convoSdk.ConversationDetail) => void = () => {};
    vi.spyOn(convoSdk, "getConversation").mockImplementation((threadId: string) =>
      threadId === THREAD_ID
        ? Promise.resolve(CONVO)
        : new Promise<convoSdk.ConversationDetail>((resolve) => {
            resolveB = resolve;
          }),
    );
    const messagesSpy = vi.spyOn(sessionsSdk, "getSessionMessages").mockResolvedValue([]);

    const navRef: { current: NavigateFunction | null } = { current: null };
    function NavCapture() {
      navRef.current = useNavigate();
      return null;
    }
    render(
      <MemoryRouter initialEntries={[`/runs/${THREAD_ID}/${RUN_1}`]}>
        <AuthProvider>
          <App>
            <NavCapture />
            <Routes>
              <Route path="/runs/:threadId/:runId" element={<RunDetail />} />
            </Routes>
          </App>
        </AuthProvider>
      </MemoryRouter>,
    );

    // Thread A settles: history loads under A's own tenant.
    await waitFor(() => expect(messagesSpy).toHaveBeenCalledWith(THREAD_ID, TENANT_ID));

    // Switch threads in place while B's tenant is still unknown.
    await act(async () => {
      navRef.current?.(`/runs/${THREAD_B}/${RUN_B}`);
    });
    // The flip frame: without the thread tag this had already fired.
    expect(messagesSpy).not.toHaveBeenCalledWith(THREAD_B, TENANT_ID);

    // B's lookup settles → history loads under B's tenant, and never under A's.
    await act(async () => {
      resolveB({ ...CONVO, thread_id: THREAD_B, tenant_id: TENANT_B });
    });
    await waitFor(() => expect(messagesSpy).toHaveBeenCalledWith(THREAD_B, TENANT_B));
    expect(messagesSpy).not.toHaveBeenCalledWith(THREAD_B, TENANT_ID);
  });
});

/** Matches the PageHeader title's ``${run_id.slice(0, 12)}…`` truncation. */
function t_runId(runId: string): string {
  return `${runId.slice(0, 12)}…`;
}
