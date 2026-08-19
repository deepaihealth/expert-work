/**
 * PlaygroundTab tests — the debug console's assembly-level behaviour.
 *
 * Both async paths are mocked: ``createSession`` returns a stubbed
 * thread, ``streamRun`` is an async generator we drive frame-by-frame
 * from the test body. This keeps the network layer out of jsdom.
 *
 * 调试台重设计 PR-A Task 19 — this file used to hold 54 ``it``s covering the
 * whole (single-component) Playground. The console shell split it into
 * ``components/console/*``, so 13 of those moved to their new owner's test
 * (``WorkspacePanel`` / ``Composer`` / ``AttachmentChips`` / the right rail's
 * panel / ``useRunTrace`` / ``RowDetailTiming`` / ``trace_match``) and the
 * remaining 41 stayed here, rewritten against the new DOM. The plan's
 * 「行为清单迁移表」 is the row-by-row ledger.
 *
 * PR-A.2 Task 11(spec §九)—— 右栏退役、轨迹进中栏第二个视图 tab:引用右栏
 * 的三条改打 ``console-view-tab-*`` / 账本行 / 详情,联动两条重写,另加一条
 * 「三 tab 互斥 + 输入区三处都在」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter } from "react-router-dom";
import "../../i18n";
import i18n from "../../i18n";

import * as approvalsSdk from "../../api/approvals";
import { ApiError, setStoredToken } from "../../api/client";
import * as planSdk from "../../api/plan";
import * as rateCardSdk from "../../api/rate_card";
import * as runsSdk from "../../api/runs";
import * as sessionsSdk from "../../api/sessions";
import * as workspaceSdk from "../../api/workspace";
import * as artifactsSdk from "../../api/artifacts";
import * as traceFacadeSdk from "../../api/trace_facade";
import * as triggersSdk from "../../api/triggers";
import * as uploadsSdk from "../../api/uploads";
import { PlaygroundTab } from "../agent_detail/PlaygroundTab";
import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";

// Track C W2 — 切入态只读:mock 掉判定 hook,两态断言直接翻转返回值。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));
import type { AgentDetailResponse } from "../../api/agents";
import type { ApprovalItem } from "../../api/approvals";
import type { SseEvent, ThreadMeta } from "../../api/sessions";
import type { FireNowResult } from "../../api/triggers";

const sampleDetail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "demo-agent",
    version: "1.0.0",
    status: "active",
    spec_sha256: "abc",
    created_by: "u",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
    spec: {},
  },
};

/** A jinja agent declaring one required prompt variable (``persona``). */
const jinjaDetail: AgentDetailResponse = {
  record: {
    ...sampleDetail.record,
    // 真实 API 形状:record.spec 是完整 manifest,不是内层 spec
    // (后端 record.spec.metadata.labels 直接取用;#824 的 fixture 造错了形状,
    // 把 PlaygroundTab 多包一层壳的 bug 盖住了整整一版)。
    spec: {
      apiVersion: "expert_work.io/v1",
      kind: "Agent",
      metadata: { name: "demo-agent", version: "1.0.0", tenant: "acme" },
      spec: {
        system_prompt: {
          template: "你是 {{ persona }}",
          jinja: true,
          variables: [{ name: "persona", trusted: true, required: true }],
        },
      },
    },
  },
};

const sampleThread: ThreadMeta = {
  thread_id: "33333333-3333-3333-3333-333333333333",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  agent_name: "demo-agent",
  agent_version: "1.0.0",
  user_id: null,
  status: "active",
  title: null,
  created_by: "u",
  created_at: "2026-05-25T00:00:00Z",
  updated_at: "2026-05-25T00:00:00Z",
};

const createSessionMock = vi.spyOn(sessionsSdk, "createSession");
const streamRunMock = vi.spyOn(sessionsSdk, "streamRun");
const uploadImageMock = vi.spyOn(uploadsSdk, "uploadImage");
const uploadDocumentMock = vi.spyOn(uploadsSdk, "uploadDocument");
const getWorkspaceMock = vi.spyOn(workspaceSdk, "getUserWorkspace");
const getWorkspaceFilesMock = vi.spyOn(workspaceSdk, "getUserWorkspaceFiles");
const downloadArtifactMock = vi.spyOn(artifactsSdk, "downloadArtifact");
const listSessionsMock = vi.spyOn(sessionsSdk, "listSessions");
const getMessagesMock = vi.spyOn(sessionsSdk, "getSessionMessages");
const listRateCardsMock = vi.spyOn(rateCardSdk, "listRateCards");
const listApprovalsMock = vi.spyOn(approvalsSdk, "listApprovals");
const decideApprovalsMock = vi.spyOn(approvalsSdk, "decideApprovals");
const streamRunEventsMock = vi.spyOn(runsSdk, "streamRunEvents");
const getRunMock = vi.spyOn(runsSdk, "getRun");
const getRunTraceMock = vi.spyOn(traceFacadeSdk, "getRunTrace");
const listThreadRunsMock = vi.spyOn(runsSdk, "listThreadRuns");
const fireTriggerNowMock = vi.spyOn(triggersSdk, "fireTriggerNow");
const getThreadPlanMock = vi.spyOn(planSdk, "getThreadPlan");

// jsdom has no IntersectionObserver — stub one that treats every observed
// element as immediately visible (fires its callback synchronously from
// ``observe``), so a history row's lazy replay kicks off without needing to
// simulate real scrolling.
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

beforeEach(() => {
  vi.unstubAllEnvs();
  // PlanCard persists its collapsed state to localStorage; clear it so a
  // prior test's toggle doesn't leak into the next test's initial render.
  window.localStorage.clear();
  createSessionMock.mockReset();
  streamRunMock.mockReset();
  uploadImageMock.mockReset();
  uploadDocumentMock.mockReset();
  getWorkspaceMock.mockReset();
  getWorkspaceMock.mockResolvedValue({ workspace: null, artifacts: [] });
  getWorkspaceFilesMock.mockReset();
  getWorkspaceFilesMock.mockResolvedValue([]);
  downloadArtifactMock.mockReset();
  downloadArtifactMock.mockResolvedValue("report.md");
  listSessionsMock.mockReset();
  listSessionsMock.mockResolvedValue([]);
  getMessagesMock.mockReset();
  getMessagesMock.mockResolvedValue([]);
  listRateCardsMock.mockReset();
  listRateCardsMock.mockResolvedValue([]);
  listApprovalsMock.mockReset();
  listApprovalsMock.mockResolvedValue({
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
  });
  decideApprovalsMock.mockReset();
  decideApprovalsMock.mockResolvedValue({ results: [], succeeded: 0 });
  streamRunEventsMock.mockReset();
  streamRunEventsMock.mockReturnValue(makeStream([]));
  // The right rail's trajectory panel fetches every shown turn's trace
  // (R16) — default to "no trace", so no test needs to care unless it does.
  getRunMock.mockReset();
  getRunMock.mockResolvedValue({
    run_id: "run-default",
    thread_id: sampleThread.thread_id,
    status: "success",
    pending_approval: null,
    trace_id: null,
  });
  getRunTraceMock.mockReset();
  getRunTraceMock.mockResolvedValue({ status: "no_trace" });
  // The task card's baseline GET (usePlanCard) — no plan by default.
  getThreadPlanMock.mockReset();
  getThreadPlanMock.mockResolvedValue(null);
  // Default: no runs for a resumed thread — a mismatch against any non-empty
  // ``history`` (the common case in the existing resume tests below), so
  // ``buildHistoryTurns`` returns null and those tests keep exercising the
  // pre-existing flat-text degradation path unless a test opts into runs.
  listThreadRunsMock.mockReset();
  listThreadRunsMock.mockResolvedValue([]);
  fireTriggerNowMock.mockReset();
  // Track C W2 — 默认 home 态;切入态测试自行翻 true(clearAllMocks 不清
  // mockReturnValue,这里显式归位防串台)。
  isTenantSwitchedMock.mockReturnValue(false);
  vi.stubGlobal("IntersectionObserver", IOStub);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  setStoredToken(null);
});

function makeStream(events: SseEvent[]): AsyncGenerator<SseEvent, void, void> {
  return (async function* () {
    for (const e of events) yield e;
  })();
}

/** An externally-resolvable promise — lets a test resolve two racing async
 *  fetches in a deliberate order (used by the stale-resume guard test). */
function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function jwt(roles: string[] = []): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(
    JSON.stringify({
      sub: "u",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      roles,
    }),
  );
  return `${header}.${body}.`;
}

function tree(detail: AgentDetailResponse) {
  return (
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          {/* antd App — SessionSidebar / PlanCard / ToolCallCard all take
              ``message`` from ``App.useApp()``. */}
          <App>
            <PlaygroundTab detail={detail} />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>
  );
}

// The per-turn run-detail link uses react-router <Link>, so every render needs
// a Router context. Batch 4b item 15's Langfuse deep link is system_admin
// gated (useAuth()), so every render also needs an AuthProvider — default to
// a non-admin token since most of these tests don't exercise the Langfuse link.
function renderPg(
  detail: AgentDetailResponse = sampleDetail,
  { admin = false }: { admin?: boolean } = {},
) {
  setStoredToken(jwt(admin ? ["system_admin"] : []));
  return render(tree(detail));
}

// Lazy thread creation — no backend session exists until the first action.
// Tests that need a live thread (a run in the transcript, the session header
// id) establish one by sending a throwaway message.
async function establishThread(user: ReturnType<typeof userEvent.setup>) {
  streamRunMock.mockReturnValue(
    makeStream([
      { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
    ]),
  );
  await user.type(await screen.findByTestId("playground-input"), "hi");
  await user.click(screen.getByTestId("playground-run"));
  await screen.findByTestId("console-thread-id");
}

/** Find text inside the MIDDLE column only. The right rail renders the same
 *  turn's trajectory (its ``assistant`` row carries the same answer text), so
 *  a bare ``screen.findByText`` on an answer is ambiguous by design now. */
function findInTranscript(text: string): Promise<HTMLElement> {
  return within(screen.getByTestId("playground-transcript")).findByText(text);
}

/** Resume a past session from the left rail — the retired session drawer's
 *  open-then-pick is one click now. */
async function resumeFromSidebar(
  user: ReturnType<typeof userEvent.setup>,
  threadId: string,
) {
  await user.click(await screen.findByTestId(`console-session-item-${threadId}`));
}

describe("PlaygroundTab", () => {
  it("does not create a thread on mount; creates it lazily on first send", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
      ]),
    );
    renderPg();
    await screen.findByTestId("playground-input");
    // No backend session yet — eager creation used to POST an empty throwaway
    // thread here (the ``listSessions`` +1-per-open bug).
    expect(createSessionMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("playground-empty-log")).toBeInTheDocument();

    // The first send creates the thread.
    await user.type(screen.getByTestId("playground-input"), "hi");
    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() => {
      expect(createSessionMock).toHaveBeenCalledWith({
        agent_name: "demo-agent",
        agent_version: "1.0.0",
      });
    });
  });

  it("streams events from streamRun and renders the answer in the turn block", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "metadata",
          data: { run_id: "r-1" },
          rawData: "",
          receivedAt: "2026-05-25T00:00:01Z",
        },
        {
          id: "2",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "hi" }] } },
          rawData: "",
          receivedAt: "2026-05-25T00:00:02Z",
        },
        {
          id: "3",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "hello");
    await user.click(screen.getByTestId("playground-run"));

    const turn = await screen.findByTestId("console-turn");
    expect(within(turn).getByTestId("playground-turn-answer")).toHaveTextContent(
      "hi",
    );
    expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument();
  });

  // §八.2 —— 会话级状态栏搬到中栏头部下方一条细行(底部输入区那份取消);
  // 一轮都没有 / 有轮次但一步都没跑成时整行不渲染(否则留一条空描边行)。
  it("puts the session stats chips in a row under the main header, not in the composer", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const frames = (stepCount: number | null) => [
      {
        id: "1",
        event: "metadata",
        data: { run_id: "r-1" },
        rawData: "",
        receivedAt: "2026-05-25T00:00:01Z",
      },
      {
        id: "2",
        event: "updates",
        data: {
          agent: {
            messages: [{ type: "ai", content: "hi" }],
            ...(stepCount === null ? {} : { step_count: stepCount }),
          },
        },
        rawData: "",
        receivedAt: "2026-05-25T00:00:02Z",
      },
      {
        id: "3",
        event: "end",
        data: "ok",
        rawData: "ok",
        receivedAt: "2026-05-25T00:00:03Z",
      },
    ];
    streamRunMock
      .mockReturnValueOnce(makeStream(frames(null)))
      .mockReturnValueOnce(makeStream(frames(1)));
    renderPg();
    await screen.findByTestId("playground-input");
    // 一轮都没有 —— 整条行不渲染。
    expect(screen.queryByTestId("console-stats-row")).not.toBeInTheDocument();

    // 第一轮没跑出任何一步(computeSessionStats 不计这种轮):有轮块,但状态
    // 栏仍是 null —— 容器也不能渲染,不然是条空白描边行。
    await user.type(screen.getByTestId("playground-input"), "hello");
    await user.click(screen.getByTestId("playground-run"));
    await screen.findByTestId("console-turn");
    expect(screen.queryByTestId("console-stats-row")).not.toBeInTheDocument();

    // 第二轮真跑了一步 —— 这条行出现在转录区**之前**(即头部下方);输入区
    // 在转录区之后,所以这同时钉死了「底部那份没了」。
    await user.type(screen.getByTestId("playground-input"), "again");
    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() => {
      expect(screen.getAllByTestId("console-turn")).toHaveLength(2);
    });

    const row = await screen.findByTestId("console-stats-row");
    expect(within(row).getByTestId("console-stats-bar")).toBeInTheDocument();
    expect(within(row).getByTestId("console-stat-turns")).toHaveTextContent("1");
    expect(
      row.compareDocumentPosition(screen.getAllByTestId("console-turn")[0]) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getAllByTestId("console-stats-bar")).toHaveLength(1);
  });

  it("renders an inline download for an artifact the turn registered", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "metadata",
          data: { run_id: "r-1" },
          rawData: "",
          receivedAt: "",
        },
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
                    {
                      id: "c1",
                      name: "save_artifact",
                      args: { name: "report.pdf", kind: "document" },
                      type: "tool_call",
                    },
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
                {
                  type: "tool",
                  tool_call_id: "c1",
                  name: "save_artifact",
                  content: "Saved artifact 'report.pdf' …",
                  status: "success",
                },
              ],
            },
          },
          rawData: "",
          receivedAt: "",
        },
        { id: "4", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
      ]),
    );
    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "make a pdf");
    await user.click(screen.getByTestId("playground-run"));

    const btn = await screen.findByTestId("playground-turn-artifact-download");
    expect(btn).toHaveTextContent("report.pdf");
    await user.click(btn);
    expect(downloadArtifactMock).toHaveBeenCalledWith("report.pdf", undefined, undefined);
  });

  it("exports the turn's authoritative event stream as JSON", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "metadata",
          data: { run_id: "r-1" },
          rawData: "",
          receivedAt: "t1",
        },
        { id: "2", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
      ]),
    );
    // The authoritative ``/events`` replay returns the full persisted stream —
    // including frames the live client may never have received.
    streamRunEventsMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "metadata",
          data: { run_id: "r-1" },
          rawData: "",
          receivedAt: "t1",
        },
        {
          id: "2",
          event: "updates",
          data: { tools: { pending_approval: "x" } },
          rawData: "",
          receivedAt: "t2",
        },
        { id: "3", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
      ]),
    );
    const createUrl = vi.fn(() => "blob:mock");
    (URL as unknown as { createObjectURL: () => string }).createObjectURL =
      createUrl;
    (
      URL as unknown as { revokeObjectURL: (u: string) => void }
    ).revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "hi");
    await user.click(screen.getByTestId("playground-run"));
    await user.click(await screen.findByTestId("playground-export-json"));

    await waitFor(() => expect(streamRunEventsMock).toHaveBeenCalled());
    // Pulled the authoritative stream for this run, not the client frames.
    expect(streamRunEventsMock.mock.calls[0][1]).toBe("r-1");
    // A JSON blob was created + a download triggered.
    expect(createUrl).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    clickSpy.mockRestore();
  });

  // 迁移表 432 的留守条:工作区内容本身归 WorkspacePanel.test,这里只钉
  // 「中栏能切到工作区视图」这条组装接线(§九「壳」:右栏退役,工作区成了
  // 中栏第三个 tab)。
  it("switches the middle column to the workspace view", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    getWorkspaceFilesMock.mockResolvedValue([{ path: "report.pdf", size: 2048 }]);
    renderPg();
    await screen.findByTestId("playground-input");
    // 初值 = 「对话」:轨迹与工作区都还没挂。
    expect(screen.getByTestId("playground-transcript")).toBeInTheDocument();
    expect(screen.queryByTestId("console-trajectory-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("playground-workspace")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("console-view-tab-workspace"));

    const panel = await screen.findByTestId("playground-workspace");
    expect(panel).toHaveTextContent("report.pdf");
    expect(screen.queryByTestId("playground-transcript")).not.toBeInTheDocument();
  });

  // §九「壳」—— 三个视图两两互斥,`console-view-tabs` 是唯一的开关;输入区
  // (Composer / 附件 / 变量)在三个 tab 下都钉在底部。
  it("the three view tabs swap the main body and keep the composer pinned in all of them", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    renderPg();
    await screen.findByTestId("playground-input");
    expect(screen.getByTestId("console-view-tabs")).toBeInTheDocument();

    await user.click(screen.getByTestId("console-view-tab-trajectory"));
    expect(screen.getByTestId("console-trajectory-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("playground-transcript")).not.toBeInTheDocument();
    expect(screen.queryByTestId("playground-workspace")).not.toBeInTheDocument();
    // 轨迹 tab 下照样能发送 / 停止 —— 输入区没跟着 Transcript 一起消失。
    expect(screen.getByTestId("playground-input")).toBeInTheDocument();
    expect(screen.getByTestId("playground-run")).toBeInTheDocument();

    await user.click(screen.getByTestId("console-view-tab-workspace"));
    expect(screen.queryByTestId("console-trajectory-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-input")).toBeInTheDocument();

    await user.click(screen.getByTestId("console-view-tab-chat"));
    expect(screen.getByTestId("playground-transcript")).toBeInTheDocument();
    expect(screen.queryByTestId("console-trajectory-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-input")).toBeInTheDocument();
  });

  it("shows a stream-failure alert when streamRun throws", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockImplementation(() => {
      return (async function* () {
        throw new Error("boom");
      })();
    });
    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "x");
    await user.click(screen.getByTestId("playground-run"));
    const alert = await screen.findByTestId("playground-turn-error");
    expect(alert).toHaveTextContent("boom");
  });

  it("shows a session-failure alert when the lazy createSession rejects", async () => {
    const user = userEvent.setup();
    createSessionMock.mockRejectedValue(
      new ApiError("agent not active", "AGENT_NOT_FOUND", 422),
    );
    renderPg();
    // Lazy — the session is created on the first send, so the failure surfaces
    // then (not on mount).
    await user.type(await screen.findByTestId("playground-input"), "hi");
    await user.click(screen.getByTestId("playground-run"));
    const alert = await screen.findByTestId("playground-session-error");
    expect(alert).toHaveTextContent("AGENT_NOT_FOUND");
  });

  it("disables Run while the input is empty", async () => {
    createSessionMock.mockResolvedValue(sampleThread);
    renderPg();
    await screen.findByTestId("playground-input");
    expect(screen.getByTestId("playground-run")).toBeDisabled();
  });

  it("uploads an attached image and sends its ref with the run", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    uploadImageMock.mockResolvedValue("expert_work://image/img-1.png");
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    renderPg();
    await screen.findByTestId("playground-input");

    const file = new File(["\x89PNG"], "shot.png", { type: "image/png" });
    await user.upload(screen.getByTestId("playground-file-input"), file);

    expect(
      await screen.findByTestId("playground-attachment"),
    ).toHaveTextContent("shot.png");
    expect(uploadImageMock).toHaveBeenCalledWith(sampleThread.thread_id, file);

    await user.type(screen.getByTestId("playground-input"), "describe this");
    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() =>
      expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument(),
    );

    expect(streamRunMock).toHaveBeenCalledWith(
      sampleThread.thread_id,
      { input: "describe this", image_refs: ["expert_work://image/img-1.png"] },
      expect.objectContaining({ signal: expect.anything() }),
    );
    // The turn consumed the attachment — chip is cleared afterward.
    expect(
      screen.queryByTestId("playground-attachment"),
    ).not.toBeInTheDocument();
  });

  it("uploads a document and surfaces its workspace path in the run prompt", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    uploadDocumentMock.mockResolvedValue("uploads/report.pdf");
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    renderPg();
    await screen.findByTestId("playground-input");

    const file = new File(["%PDF-1.4"], "report.pdf", {
      type: "application/pdf",
    });
    await user.upload(screen.getByTestId("playground-doc-input"), file);

    expect(
      await screen.findByTestId("playground-attachment"),
    ).toHaveTextContent("report.pdf");
    expect(uploadDocumentMock).toHaveBeenCalledWith(
      sampleThread.thread_id,
      file,
    );

    await user.type(screen.getByTestId("playground-input"), "summarize it");
    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() =>
      expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument(),
    );

    // The doc path is prepended to the prompt (no image_refs for a doc-only turn).
    const [, body] = streamRunMock.mock.calls.at(-1) ?? [];
    expect((body as { input: string }).input).toContain("uploads/report.pdf");
    expect((body as { input: string }).input).toContain("summarize it");
    expect((body as { image_refs?: unknown }).image_refs).toBeUndefined();
  });

  it("renders declared prompt variables and sends their values as inputs", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    renderPg(jinjaDetail);
    await screen.findByTestId("playground-input");

    await user.type(screen.getByTestId("playground-var-persona"), "顾问");
    await user.type(screen.getByTestId("playground-input"), "go");
    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() =>
      expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument(),
    );

    expect(streamRunMock).toHaveBeenCalledWith(
      sampleThread.thread_id,
      { input: "go", inputs: { persona: "顾问" } },
      expect.objectContaining({ signal: expect.anything() }),
    );
  });

  // NEW (Task 19 ①) — R5/R7 的输入闸:必填变量没填,发送按钮就是灰的。
  it("keeps Run disabled while a required prompt variable is empty", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        { id: "1", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
      ]),
    );
    renderPg(jinjaDetail);
    await screen.findByTestId("playground-input");

    await user.type(screen.getByTestId("playground-input"), "go");
    // Text present, but ``persona`` is still empty → still disabled.
    expect(screen.getByTestId("playground-run")).toBeDisabled();

    await user.type(screen.getByTestId("playground-var-persona"), "顾问");
    expect(screen.getByTestId("playground-run")).toBeEnabled();

    await user.click(screen.getByTestId("playground-run"));
    await waitFor(() => expect(streamRunMock).toHaveBeenCalled());
  });

  // NEW (Task 19 ②) — R11:变量值随会话,切会话必须清空。
  it("clears prompt-variable values when switching to another session", async () => {
    const user = userEvent.setup();
    const past: ThreadMeta = {
      ...sampleThread,
      thread_id: "aaaaaaaa-0000-0000-0000-0000000000v1",
    };
    listSessionsMock.mockResolvedValue([past]);
    renderPg(jinjaDetail);
    await screen.findByTestId("playground-input");

    await user.type(screen.getByTestId("playground-var-persona"), "顾问");
    expect(screen.getByTestId("playground-var-persona")).toHaveValue("顾问");

    await resumeFromSidebar(user, past.thread_id);

    await waitFor(() =>
      expect(screen.getByTestId("playground-var-persona")).toHaveValue(""),
    );
  });

  it("does not treat a bare inner spec as a jinja agent (record.spec is the full manifest)", async () => {
    // 如果有人把 record.spec 造成内层 spec(旧 fixture 的形状),变量框不能出现——
    // 这条守住「readers 读的是 manifest.spec.system_prompt」这个约定。
    const innerShape: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        spec: {
          system_prompt: {
            template: "你是 {{ persona }}",
            jinja: true,
            variables: [{ name: "persona" }],
          },
        },
      },
    };
    renderPg(innerShape);
    await screen.findByTestId("playground-input");
    expect(screen.queryByTestId("playground-vars")).not.toBeInTheDocument();
  });

  it("shows an upload-error alert and keeps Run usable when upload fails", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    uploadImageMock.mockRejectedValue(
      new ApiError("too big", "IMAGE_TOO_LARGE", 413),
    );
    renderPg();
    await screen.findByTestId("playground-input");

    const file = new File(["x"], "huge.png", { type: "image/png" });
    await user.upload(screen.getByTestId("playground-file-input"), file);

    const alert = await screen.findByTestId("playground-upload-error");
    expect(alert).toHaveTextContent("IMAGE_TOO_LARGE");
    expect(
      screen.queryByTestId("playground-attachment"),
    ).not.toBeInTheDocument();
  });

  it("shows a workspace-full alert (not the raw ApiError string) when document upload hits 429", async () => {
    // Locale-sensitive assertion — pin zh-CN explicitly and restore
    // afterward so it doesn't leak into later tests in this file (the
    // i18n singleton persists its resolved language across `it` blocks).
    const priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
    try {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      uploadDocumentMock.mockRejectedValue(
        new ApiError("workspace is full — delete files to free space", "HTTP_429", 429),
      );
      renderPg();
      await screen.findByTestId("playground-input");

      const file = new File(["%PDF-1.4"], "report.pdf", {
        type: "application/pdf",
      });
      await user.upload(screen.getByTestId("playground-doc-input"), file);

      const alert = await screen.findByTestId("playground-upload-error");
      expect(alert).toHaveTextContent("工作区已满");
      expect(
        screen.queryByTestId("playground-attachment"),
      ).not.toBeInTheDocument();
    } finally {
      await i18n.changeLanguage(priorLang);
    }
  });

  it("accumulates turns across runs and parses per-turn token usage", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const endFrame = (text: string, input: number): SseEvent[] => [
      {
        id: "u",
        event: "updates",
        data: {
          agent: {
            messages: [
              {
                type: "ai",
                content: text,
                usage_metadata: {
                  input_tokens: input,
                  output_tokens: 10,
                  total_tokens: input + 10,
                },
              },
            ],
          },
        },
        rawData: "",
        receivedAt: "2026-05-25T00:00:02Z",
      },
      {
        id: "e",
        event: "end",
        data: "ok",
        rawData: "ok",
        receivedAt: "2026-05-25T00:00:03Z",
      },
    ];
    streamRunMock.mockReturnValueOnce(
      makeStream(endFrame("first answer", 100)),
    );
    streamRunMock.mockReturnValueOnce(
      makeStream(endFrame("second answer", 200)),
    );

    renderPg();
    await screen.findByTestId("playground-input");

    await user.type(screen.getByTestId("playground-input"), "q1");
    await user.click(screen.getByTestId("playground-run"));
    await findInTranscript("first answer");

    await user.type(screen.getByTestId("playground-input"), "q2");
    await user.click(screen.getByTestId("playground-run"));
    await findInTranscript("second answer");

    // Both turns persist (not wiped) + the one-line footer meta (§八.4)
    // renders per turn with the token total.
    expect(screen.getAllByTestId("console-turn")).toHaveLength(2);
    const metas = screen.getAllByTestId("console-footer-meta");
    expect(metas).toHaveLength(2);
    for (const m of metas) expect(m).toHaveTextContent(/tok/);
    // The thread is reused across turns (multi-turn continuation).
    expect(
      streamRunMock.mock.calls.every(([tid]) => tid === sampleThread.thread_id),
    ).toBe(true);
  });

  // #10 — per-turn retry: the button re-dispatches streamRun with the
  // original turn's request as a NEW turn.
  it("retries a turn with the same input via the per-turn retry button", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const frames = (text: string): SseEvent[] => [
      {
        id: "u",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: text }] } },
        rawData: "",
        receivedAt: "2026-05-25T00:00:02Z",
      },
      {
        id: "e",
        event: "end",
        data: "ok",
        rawData: "ok",
        receivedAt: "2026-05-25T00:00:03Z",
      },
    ];
    streamRunMock.mockReturnValueOnce(makeStream(frames("first answer")));
    streamRunMock.mockReturnValueOnce(makeStream(frames("retried answer")));

    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "q1");
    await user.click(screen.getByTestId("playground-run"));
    await findInTranscript("first answer");

    await user.click(screen.getByTestId("playground-turn-retry"));
    await findInTranscript("retried answer");

    // Re-dispatched to the same thread with the exact same request body.
    expect(streamRunMock).toHaveBeenCalledTimes(2);
    expect(streamRunMock.mock.calls[1][0]).toBe(sampleThread.thread_id);
    expect(streamRunMock.mock.calls[1][1]).toEqual(
      streamRunMock.mock.calls[0][1],
    );
    // The retry appended a new turn — the original stays in the transcript.
    expect(screen.getAllByTestId("console-turn")).toHaveLength(2);
  });

  // Cross-tenant W4(D2)— rate_card 是 system_admin-only:非 admin 挂载
  // 调试台不再发 listRateCards(否则每次都吃一发静默 403)。
  it("does not fetch rate cards for a non-admin user", async () => {
    const costDetail: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        // 真实 API 形状:record.spec 是完整 manifest,不是内层 spec。
        spec: {
          apiVersion: "expert_work.io/v1",
          kind: "Agent",
          metadata: { name: "demo-agent", version: "1.0.0", tenant: "acme" },
          spec: { model: { provider: "anthropic", name: "claude-x" } },
        },
      },
    };
    renderPg(costDetail);
    await screen.findByTestId("playground-input");
    expect(listRateCardsMock).not.toHaveBeenCalled();
  });

  // §八.4 — 脚注一行式:cost lives in the meta tooltip;§八.6 — 「查看运行」
  // 搬到右栏头部,这里连同断言(console-inspect-run-link)。
  it("shows per-turn cost (tooltip) + step count in the one-line footer", async () => {
    const user = userEvent.setup();
    const costDetail: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        spec: {
          apiVersion: "expert_work.io/v1",
          kind: "Agent",
          metadata: { name: "demo-agent", version: "1.0.0", tenant: "acme" },
          spec: { model: { provider: "anthropic", name: "claude-x" } },
        },
      },
    };
    createSessionMock.mockResolvedValue(sampleThread);
    listRateCardsMock.mockResolvedValue([
      {
        id: "rc",
        tenant_id: null,
        provider: "anthropic",
        model: "claude-x",
        input_per_mtok_micros: 3_000_000,
        output_per_mtok_micros: 15_000_000,
        cache_creation_per_mtok_micros: 0,
        cache_read_per_mtok_micros: 0,
      },
    ]);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "m",
          event: "metadata",
          data: { run_id: "run-77" },
          rawData: "",
          receivedAt: "2026-05-25T00:00:01Z",
        },
        {
          id: "u",
          event: "updates",
          data: {
            agent: {
              messages: [
                {
                  type: "ai",
                  content: "hi",
                  usage_metadata: {
                    input_tokens: 1000,
                    output_tokens: 100,
                    total_tokens: 1100,
                  },
                },
              ],
              step_count: 2,
            },
          },
          rawData: "",
          receivedAt: "2026-05-25T00:00:02Z",
        },
        {
          id: "e",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    // W4(D2)— rate_card 拉取只对 system_admin 发起,成本区仅 admin 可见。
    renderPg(costDetail, { admin: true });
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "q");
    await user.click(screen.getByTestId("playground-run"));
    await screen.findByTestId("console-turn");

    const meta = await screen.findByTestId("console-footer-meta");
    expect(meta).toHaveTextContent(/2 步|2 steps/);
    await user.hover(meta);
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent(/≈ ¥/);

    // §九「详情」— 「查看运行」的新家:轨迹 tab 里点开一条记录,详情「概要」
    // 里的 Run 链接(右栏头部退役,链接跟着那条记录的 runId 走)。
    await user.click(screen.getByTestId("console-view-tab-trajectory"));
    await user.click(screen.getAllByTestId("console-traj-row")[0]);
    const runLink = await screen.findByTestId("console-inspect-run-link");
    expect(runLink).toHaveAttribute("href", `/runs/${sampleThread.thread_id}/run-77`);
  });

  // R7 — 「已恢复」提示条退役(左栏选中态已表达「你在哪个会话」);这条改钉
  // 「左栏直接列出会话,点一下就拉历史」。
  it("lists past sessions in the left rail and loads one on click", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const past: ThreadMeta = {
      ...sampleThread,
      thread_id: "99999999-9999-9999-9999-999999999999",
      created_at: "2026-05-20T00:00:00Z",
    };
    listSessionsMock.mockResolvedValue([past]);
    getMessagesMock.mockResolvedValue([
      { role: "user", content: "earlier question" },
      { role: "assistant", content: "earlier answer" },
    ]);
    renderPg();
    await screen.findByTestId("playground-input");

    await resumeFromSidebar(user, past.thread_id);

    // Prior conversation loaded from the checkpoint and rendered read-only.
    const hist = await screen.findByTestId("playground-history");
    expect(hist).toHaveTextContent("earlier question");
    expect(hist).toHaveTextContent("earlier answer");
    // Home state — the ambient scope resolves to no tenant override.
    expect(getMessagesMock).toHaveBeenCalledWith(past.thread_id, undefined);
  });

  it("loads the workspace inspector without a thread (user-scoped)", async () => {
    // The whole point of the user-scoped route: the panel shows the current
    // user's workspace with no session bound — so it survives session
    // deletion. No establishThread() here. (The panel only mounts on its own
    // tab — the middle column keeps the inactive views unmounted — so the
    // switch is the trigger, not the page mount.)
    const user = userEvent.setup();
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: "t1",
        user_id: "u1",
        volume_name: "expert-work-ws-t-u",
        size_bytes: 2048,
        size_limit_bytes: 1_048_576,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [],
    });
    getWorkspaceFilesMock.mockResolvedValue([{ path: "out.txt", size: 11 }]);
    renderPg();
    await screen.findByTestId("playground-input");
    await user.click(screen.getByTestId("console-view-tab-workspace"));

    const panel = await screen.findByTestId("playground-workspace");
    expect(panel).toHaveTextContent("expert-work-ws-t-u");
    expect(panel).toHaveTextContent("out.txt");
    // ...and the load was keyed on the caller, not a thread. W3 — trailing
    // (userId, tenantScope) both undefined in the home state.
    expect(getWorkspaceMock).toHaveBeenCalledWith(undefined, undefined);
    expect(createSessionMock).not.toHaveBeenCalled();
  });

  it("surfaces an approval gate, approves, and streams the continuation", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    // Paused run: an AI tool_call with no final text → detectApproval polls.
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "m",
          event: "metadata",
          data: { run_id: "r-pause" },
          rawData: "",
          receivedAt: "2026-05-25T00:00:01Z",
        },
        {
          id: "u",
          event: "updates",
          data: {
            agent: {
              messages: [
                {
                  type: "ai",
                  content: "",
                  tool_calls: [
                    {
                      id: "tc1",
                      name: "bash",
                      args: { cmd: "rm -rf /" },
                      type: "tool_call",
                    },
                  ],
                },
              ],
              step_count: 1,
            },
          },
          rawData: "",
          receivedAt: "2026-05-25T00:00:02Z",
        },
        {
          id: "e",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:03Z",
        },
      ]),
    );
    const approval: ApprovalItem = {
      id: "ap1",
      tenant_id: sampleThread.tenant_id,
      user_id: null,
      run_id: "r-pause",
      thread_id: sampleThread.thread_id,
      request_id: "req1",
      node: "tools",
      reason_kind: "policy_required",
      action_summary: "run bash: rm -rf /",
      proposed_args: { cmd: "rm -rf /" },
      requested_at: "2026-05-25T00:00:03Z",
      timeout_at: "2026-05-26T00:00:03Z",
      status: "pending",
      decided_by: null,
      decided_at: null,
    };
    listApprovalsMock.mockResolvedValue({
      items: [approval],
      total: 1,
      limit: 50,
      offset: 0,
    });
    decideApprovalsMock.mockResolvedValue({
      results: [{ run_id: "r-pause", ok: true, continuation_run_id: "r-cont" }],
      succeeded: 1,
    });
    streamRunEventsMock.mockReturnValue(
      makeStream([
        {
          id: "u2",
          event: "updates",
          data: {
            agent: {
              messages: [{ type: "ai", content: "done after approval" }],
            },
          },
          rawData: "",
          receivedAt: "2026-05-25T00:00:05Z",
        },
        {
          id: "e2",
          event: "end",
          data: "ok",
          rawData: "ok",
          receivedAt: "2026-05-25T00:00:06Z",
        },
      ]),
    );

    renderPg();
    await screen.findByTestId("playground-input");
    await user.type(
      screen.getByTestId("playground-input"),
      "delete everything",
    );
    await user.click(screen.getByTestId("playground-run"));

    const card = await screen.findByTestId("playground-approval");
    expect(card).toHaveTextContent("rm -rf /");

    await user.click(screen.getByTestId("playground-approval-approve"));
    await findInTranscript("done after approval");
    expect(decideApprovalsMock).toHaveBeenCalledWith([
      {
        thread_id: sampleThread.thread_id,
        run_id: "r-pause",
        decision: "approve",
      },
    ]);
    expect(streamRunEventsMock).toHaveBeenCalledWith(
      sampleThread.thread_id,
      "r-cont",
      expect.objectContaining({ signal: expect.anything() }),
    );
    expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
  });

  it("removes an attachment when its tag is closed", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    uploadImageMock.mockResolvedValue("expert_work://image/img-2.png");
    renderPg();
    await screen.findByTestId("playground-input");

    const file = new File(["x"], "pic.png", { type: "image/png" });
    await user.upload(screen.getByTestId("playground-file-input"), file);
    await screen.findByTestId("playground-attachment");

    await user.click(screen.getByLabelText("Remove attachment"));
    expect(
      screen.queryByTestId("playground-attachment"),
    ).not.toBeInTheDocument();
  });

  // SE-16 (SE-A46) — per-turn 👍/👎 feeding the skill-evolution pipeline.
  it("thumbs-up submits feedback for the turn", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const feedbackMock = vi
      .spyOn(sessionsSdk, "submitSessionFeedback")
      .mockResolvedValue({
        id: 1,
        thread_id: sampleThread.thread_id,
        rating: "up",
        turn_seq: 0,
        trace_id: null,
      });
    renderPg();
    await screen.findByTestId("playground-input");
    await establishThread(user);

    await user.click(await screen.findByTestId("playground-feedback-up"));
    await waitFor(() =>
      expect(feedbackMock).toHaveBeenCalledWith(sampleThread.thread_id, {
        rating: "up",
        comment: undefined,
        turn_seq: 0,
      }),
    );
    expect(screen.getByText("Thanks for the feedback")).toBeInTheDocument();
    // One submission per turn — both buttons disable.
    expect(screen.getByTestId("playground-feedback-down")).toBeDisabled();
  });

  it("thumbs-down opens a comment popover and submits rating+comment", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const feedbackMock = vi
      .spyOn(sessionsSdk, "submitSessionFeedback")
      .mockResolvedValue({
        id: 2,
        thread_id: sampleThread.thread_id,
        rating: "down",
        turn_seq: 0,
        trace_id: null,
      });
    renderPg();
    await screen.findByTestId("playground-input");
    await establishThread(user);

    await user.click(await screen.findByTestId("playground-feedback-down"));
    // Popover renders into a body portal.
    await user.type(
      await screen.findByTestId("playground-feedback-comment"),
      "答非所问",
    );
    await user.click(screen.getByTestId("playground-feedback-down-submit"));
    await waitFor(() =>
      expect(feedbackMock).toHaveBeenCalledWith(sampleThread.thread_id, {
        rating: "down",
        comment: "答非所问",
        turn_seq: 0,
      }),
    );
  });

  it("surfaces an inline error when feedback submission fails", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    vi.spyOn(sessionsSdk, "submitSessionFeedback").mockRejectedValue(
      new Error("boom"),
    );
    renderPg();
    await screen.findByTestId("playground-input");
    await establishThread(user);

    await user.click(await screen.findByTestId("playground-feedback-up"));
    await screen.findByTestId("playground-feedback-error");
    // Not marked submitted — the user can retry.
    expect(screen.getByTestId("playground-feedback-up")).toBeEnabled();
  });

  // Task 19 ③④ — 中栏 ↔ 右栏的两个入口(R9 / R18)。
  describe("视图联动(中栏 → 轨迹 tab)", () => {
    const TWO_STEP_EVENTS: SseEvent[] = [
      {
        id: "m",
        event: "metadata",
        data: { run_id: "run-inspect" },
        rawData: "",
        receivedAt: "t1",
      },
      {
        id: "u1",
        event: "updates",
        data: {
          agent: {
            step_count: 1,
            messages: [
              {
                type: "ai",
                content: "",
                // 有 reasoning 过程条才出 think 紧凑行 —— 「轨迹」按钮的
                // `think:<seq>` → `assistant:<seq>` 映射靠它才测得到。
                additional_kwargs: { reasoning_content: "先查一下 CRM" },
                tool_calls: [
                  { id: "c1", name: "query_crm", args: { id: "C-1" }, type: "tool_call" },
                ],
              },
            ],
          },
        },
        rawData: "",
        receivedAt: "t2",
      },
      {
        id: "u2",
        event: "updates",
        data: {
          tools: {
            messages: [
              {
                type: "tool",
                tool_call_id: "c1",
                name: "query_crm",
                content: "3 条记录",
                status: "success",
              },
            ],
          },
        },
        rawData: "",
        receivedAt: "t3",
      },
      {
        id: "u3",
        event: "updates",
        data: { agent: { step_count: 2, messages: [{ type: "ai", content: "已完成查询。" }] } },
        rawData: "",
        receivedAt: "t4",
      },
      { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t5" },
    ];

    async function runTwoTurns(user: ReturnType<typeof userEvent.setup>) {
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValueOnce(makeStream(TWO_STEP_EVENTS));
      streamRunMock.mockReturnValueOnce(
        makeStream([
          { id: "m2", event: "metadata", data: { run_id: "run-2" }, rawData: "", receivedAt: "t1" },
          {
            id: "u",
            event: "updates",
            data: { agent: { messages: [{ type: "ai", content: "第二轮答案" }] } },
            rawData: "",
            receivedAt: "t2",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
        ]),
      );
      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "q1");
      await user.click(screen.getByTestId("playground-run"));
      await findInTranscript("已完成查询。");
      await user.type(screen.getByTestId("playground-input"), "q2");
      await user.click(screen.getByTestId("playground-run"));
      await findInTranscript("第二轮答案");
    }

    /** 账本里当前选中的那一行(时间轴块与账本行共用一个选中态)。 */
    function selectedLedgerRow(): HTMLElement {
      const row = screen
        .getAllByTestId("console-traj-row")
        .find((el) => el.getAttribute("aria-selected") === "true");
      expect(row).toBeDefined();
      return row as HTMLElement;
    }

    // §九「联动」— 脚注「查看轨迹」:切到轨迹 tab + 选中该轮**最后一条**
    // ASSISTANT 记录 + 打开它的详情。
    it("the footer's 查看轨迹 switches to the trajectory view and selects that turn's last ASSISTANT record", async () => {
      const user = userEvent.setup();
      await runTwoTurns(user);
      // 起手在「对话」视图:轨迹还没挂。
      expect(screen.queryByTestId("console-trajectory-panel")).not.toBeInTheDocument();

      const firstTurn = screen.getAllByTestId("console-turn")[0];
      await user.click(within(firstTurn).getByTestId("console-turn-inspect"));

      expect(await screen.findByTestId("console-trajectory-panel")).toBeInTheDocument();
      expect(screen.queryByTestId("playground-transcript")).not.toBeInTheDocument();
      await waitFor(() => {
        const row = selectedLedgerRow();
        expect(row.dataset.kind).toBe("assistant");
        // 第 1 轮的最后一条 assistant —— 不是第 2 轮的,也不是第 1 轮第一步的。
        expect(row).toHaveTextContent("已完成查询。");
      });
      expect(await screen.findByTestId("console-detail-header")).toBeInTheDocument();

      // 回到「对话」再点第 2 轮的脚注 —— 选中跟着换轮。
      await user.click(screen.getByTestId("console-view-tab-chat"));
      const secondTurn = screen.getAllByTestId("console-turn")[1];
      await user.click(within(secondTurn).getByTestId("console-turn-inspect"));
      await waitFor(() => expect(selectedLedgerRow()).toHaveTextContent("第二轮答案"));
    });

    // §九「联动」— 过程条每行「轨迹」:切 tab + 选中**对应那条**记录;紧凑行的
    // `think:<seq>` 要落到账本的 `assistant:<seq>` 上(不是该轮最后一条)。
    it("the process strip's 轨迹 maps think:<seq> onto the ledger's assistant:<seq> record", async () => {
      const user = userEvent.setup();
      await runTwoTurns(user);

      const firstTurn = screen.getAllByTestId("console-turn")[0];
      // §八.3 — 完成后的过程条默认折叠,先展开才点得到行尾「轨迹」。
      if (!within(firstTurn).queryByTestId("console-process-steps")) {
        await user.click(within(firstTurn).getByTestId("console-process-head"));
      }
      // 第一条紧凑行 = 第 1 步的 think 行(第二条是它发起的 tool 行)。
      expect(within(firstTurn).getByTestId("console-row-think")).toBeInTheDocument();
      await user.click(within(firstTurn).getAllByTestId("console-row-inspect")[0]);

      expect(await screen.findByTestId("console-trajectory-panel")).toBeInTheDocument();
      await waitFor(() => {
        const row = selectedLedgerRow();
        // 账本记录 id = `<turnKey>/<rowId>`,尾巴就是映射后的行 id。
        expect(row.dataset.recordId).toMatch(/\/assistant:\d+$/);
        // 第 1 步没有正文(只发了工具调用)—— 落到第 2 步的 assistant 上就会
        // 写「已完成查询。」,这条断言是两者的分水岭。
        expect(row).toHaveTextContent(i18n.t("console.ledger_tool_call_only"));
      });
      expect(await screen.findByTestId("console-detail-header")).toBeInTheDocument();

      // 关掉详情、回「对话」、再点同一处:照样重开(切 tab 会把轨迹视图整个
      // 卸掉,所以这条钉的是「往返一趟还好使」;`nonce` 自增本身在视图不卸载
      // 的前提下才有意义,由 TrajectoryView.test「focusRequest 指向同一条记录
      // 的新 nonce 会再滚一次」钉住)。
      await user.click(screen.getByTestId("console-detail-close"));
      expect(screen.queryByTestId("console-detail-header")).not.toBeInTheDocument();
      await user.click(screen.getByTestId("console-view-tab-chat"));
      const turnAgain = screen.getAllByTestId("console-turn")[0];
      if (!within(turnAgain).queryByTestId("console-process-steps")) {
        await user.click(within(turnAgain).getByTestId("console-process-head"));
      }
      await user.click(within(turnAgain).getAllByTestId("console-row-inspect")[0]);
      expect(await screen.findByTestId("console-detail-header")).toBeInTheDocument();
    });
  });

  // Task 5 — resume reconstructs history as lazy read-only turn blocks when
  // the message/run counts line up (buildHistoryTurns pairs 1:1); a mismatch
  // or a failed lookup/replay must degrade — never a crash, never lost content.
  describe("history lazy rebuild on resume", () => {
    it("replays a count-matched history run into a full turn block when its row scrolls into view", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000001",
      };
      listSessionsMock.mockResolvedValue([past]);
      getMessagesMock.mockResolvedValue([
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
      ]);
      listThreadRunsMock.mockResolvedValue([
        { runId: "r1", status: "success", isResume: false, createdAt: "2026-05-25T00:00:00Z", tokens: null },
      ]);
      streamRunEventsMock.mockReturnValue(
        makeStream([
          {
            id: "u0",
            event: "updates",
            data: {
              agent: {
                step_count: 1,
                messages: [
                  {
                    type: "ai",
                    content: "",
                    tool_calls: [
                      { id: "c1", name: "query_crm", args: { id: "C-1" }, type: "tool_call" },
                    ],
                  },
                ],
              },
            },
            rawData: "",
            receivedAt: "t0",
          },
          {
            id: "u1",
            event: "updates",
            data: {
              tools: {
                messages: [
                  {
                    type: "tool",
                    tool_call_id: "c1",
                    name: "query_crm",
                    content: "3 条记录",
                    status: "success",
                  },
                ],
              },
            },
            rawData: "",
            receivedAt: "t1",
          },
          {
            id: "u2",
            event: "updates",
            data: {
              agent: { messages: [{ type: "ai", content: "replayed answer" }] },
            },
            rawData: "",
            receivedAt: "t2",
          },
          { id: "e1", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
        ]),
      );

      renderPg();
      await screen.findByTestId("playground-input");
      await resumeFromSidebar(user, past.thread_id);

      await waitFor(() =>
        expect(streamRunEventsMock).toHaveBeenCalledWith(
          past.thread_id,
          "r1",
          expect.anything(),
        ),
      );

      // The replayed answer renders (not just the flat fallback text) — and
      // the turn block's compact step rows filled in from the replayed events.
      await findInTranscript("replayed answer");
      const turn = await screen.findByTestId("console-turn");
      // §八.3 — 完成后的过程条默认折叠,展开才看得到紧凑行。
      if (!within(turn).queryByTestId("console-process-steps")) {
        await user.click(within(turn).getByTestId("console-process-head"));
      }
      expect(within(turn).getByTestId("console-row-tool")).toBeInTheDocument();
      expect(
        screen.queryByText(i18n.t("playground.history_loading")),
      ).not.toBeInTheDocument();
      // No approval control on a finished historical run.
      expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
    });

    // #10 — a history turn's retry can't re-dispatch (a past run's enqueued
    // inputs aren't exposed by the backend yet): it backfills the input box
    // for the user to re-send, and must dispatch nothing itself.
    it("backfills the input box (no re-dispatch) when a history turn's retry is clicked", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000009",
      };
      listSessionsMock.mockResolvedValue([past]);
      getMessagesMock.mockResolvedValue([
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
      ]);
      listThreadRunsMock.mockResolvedValue([
        { runId: "r1", status: "success", isResume: false, createdAt: "t1", tokens: null },
      ]);
      streamRunEventsMock.mockReturnValue(
        makeStream([
          {
            id: "u1",
            event: "updates",
            data: {
              agent: { messages: [{ type: "ai", content: "replayed answer" }] },
            },
            rawData: "",
            receivedAt: "t1",
          },
          { id: "e1", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
        ]),
      );

      renderPg();
      await screen.findByTestId("playground-input");
      await resumeFromSidebar(user, past.thread_id);
      // The retry button only appears once the row's replay settles.
      await findInTranscript("replayed answer");

      await user.click(screen.getByTestId("playground-turn-retry"));

      expect(screen.getByTestId("playground-input")).toHaveValue("q1");
      // Backfill only — no run was dispatched.
      expect(streamRunMock).not.toHaveBeenCalled();
    });

    it("falls back to the flat history block when message/run counts don't line up", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000002",
      };
      listSessionsMock.mockResolvedValue([past]);
      getMessagesMock.mockResolvedValue([
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
        { role: "user", content: "q2" },
        { role: "assistant", content: "a2" },
      ]);
      // 2 turns worth of messages, 3 runs — buildHistoryTurns' count guard
      // rejects the pairing (e.g. an approval split one turn across 2 runs).
      listThreadRunsMock.mockResolvedValue([
        { runId: "r1", status: "success", isResume: false, createdAt: "t1", tokens: null },
        { runId: "r2", status: "success", isResume: true, createdAt: "t2", tokens: null },
        { runId: "r3", status: "success", isResume: true, createdAt: "t3", tokens: null },
      ]);

      renderPg();
      await screen.findByTestId("playground-input");
      await resumeFromSidebar(user, past.thread_id);

      // Existing flat degradation block renders the raw text turns.
      const hist = await screen.findByTestId("playground-history");
      expect(hist).toHaveTextContent("q1");
      expect(hist).toHaveTextContent("a2");
      expect(
        screen.queryByText(i18n.t("playground.history_loading")),
      ).not.toBeInTheDocument();
      // The count mismatch means no replay was ever attempted.
      expect(streamRunEventsMock).not.toHaveBeenCalled();
    });

    it("keeps the fallback answer when a history run's replay fails", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000003",
      };
      listSessionsMock.mockResolvedValue([past]);
      getMessagesMock.mockResolvedValue([
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
      ]);
      listThreadRunsMock.mockResolvedValue([
        { runId: "r1", status: "success", isResume: false, createdAt: "t1", tokens: null },
      ]);
      streamRunEventsMock.mockImplementation(() => {
        return (async function* () {
          throw new Error("replay boom");
        })();
      });

      renderPg();
      await screen.findByTestId("playground-input");
      await resumeFromSidebar(user, past.thread_id);

      // Fallback answer (from ``/messages``) still shows; no crash, no
      // approval control on a failed historical replay.
      await screen.findByText("a1");
      expect(screen.getByTestId("playground-input")).toBeInTheDocument();
      expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
    });

    it("keeps the fallback answer when a history run replays empty (only an end frame)", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000004",
      };
      listSessionsMock.mockResolvedValue([past]);
      getMessagesMock.mockResolvedValue([
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
      ]);
      listThreadRunsMock.mockResolvedValue([
        { runId: "r1", status: "success", isResume: false, createdAt: "t1", tokens: null },
      ]);
      // The terminal-replay endpoint always appends an ``end`` frame, so an
      // empty run replays as a lone end frame — no renderable content.
      streamRunEventsMock.mockReturnValue(
        makeStream([
          { id: "e1", event: "end", data: "ok", rawData: "ok", receivedAt: "t1" },
        ]),
      );

      renderPg();
      await screen.findByTestId("playground-input");
      await resumeFromSidebar(user, past.thread_id);

      await waitFor(() =>
        expect(streamRunEventsMock).toHaveBeenCalledWith(
          past.thread_id,
          "r1",
          expect.anything(),
        ),
      );

      // An empty replay degrades to the fallback text (from ``/messages``) —
      // it must NOT fall through to the full render's "no text" empty state,
      // which would drop content we already have. No crash, no Spin.
      await screen.findByText("a1");
      expect(
        screen.queryByText(i18n.t("playground.turn_no_text")),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText(i18n.t("playground.history_loading")),
      ).not.toBeInTheDocument();
      expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
    });

    it("drops a stale resume's history write when a newer resume superseded it", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      const threadA: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-0000000000a1",
      };
      const threadB: ThreadMeta = {
        ...sampleThread,
        thread_id: "bbbbbbbb-0000-0000-0000-0000000000b2",
      };
      listSessionsMock.mockResolvedValue([threadA, threadB]);

      // Control each thread's message fetch independently so we can resolve
      // A *after* B (A resumed first, but its slow fetch lands last).
      const msgsA = deferred<Array<{ role: "user" | "assistant"; content: string }>>();
      const msgsB = deferred<Array<{ role: "user" | "assistant"; content: string }>>();
      getMessagesMock.mockImplementation((tid: string) =>
        tid === threadA.thread_id ? msgsA.promise : msgsB.promise,
      );
      // Runs resolve immediately per thread (Promise.all still waits on the
      // deferred messages fetch above); 1 run ↔ 1 message-turn each → paired.
      listThreadRunsMock.mockImplementation((tid: string) =>
        Promise.resolve(
          tid === threadA.thread_id
            ? [{ runId: "rA", status: "success" as const, isResume: false, createdAt: "t1", tokens: null }]
            : [{ runId: "rB", status: "success" as const, isResume: false, createdAt: "t1", tokens: null }],
        ),
      );
      // Each run's replay yields a distinct answer so we can tell whose turns
      // actually rendered; fresh generator per call (never exhausted).
      streamRunEventsMock.mockImplementation((_tid: string, runId: string) =>
        makeStream([
          {
            id: "u",
            event: "updates",
            data: {
              agent: {
                messages: [
                  { type: "ai", content: runId === "rB" ? "answer-B" : "answer-A" },
                ],
              },
            },
            rawData: "",
            receivedAt: "t1",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
        ]),
      );

      renderPg();
      await screen.findByTestId("playground-input");

      // Resume A (its message fetch stays pending), then B before A resolved —
      // B supersedes A (new AbortController).
      await resumeFromSidebar(user, threadA.thread_id);
      await resumeFromSidebar(user, threadB.thread_id);

      // Resolve B first → its history builds + its run replays.
      await act(async () => {
        msgsB.resolve([
          { role: "user", content: "qB" },
          { role: "assistant", content: "aB" },
        ]);
      });
      await findInTranscript("answer-B");

      // Now resolve the stale A LAST — the guard must drop its write.
      await act(async () => {
        msgsA.resolve([
          { role: "user", content: "qA" },
          { role: "assistant", content: "aA" },
        ]);
      });
      // Let any (incorrectly ungated) stale microtasks flush.
      await waitFor(() => expect(getMessagesMock).toHaveBeenCalledTimes(2));

      // B's history survives; A's content never clobbers it.
      expect(
        within(screen.getByTestId("playground-transcript")).getByText("answer-B"),
      ).toBeInTheDocument();
      expect(screen.queryByText("answer-A")).not.toBeInTheDocument();
      expect(screen.queryByText("qA")).not.toBeInTheDocument();
      // Only B's own run was replayed — no wrong-thread replay of A's runId
      // against B's thread_id.
      expect(streamRunEventsMock).toHaveBeenCalledWith(
        threadB.thread_id,
        "rB",
        expect.anything(),
      );
      expect(streamRunEventsMock).not.toHaveBeenCalledWith(
        threadB.thread_id,
        "rA",
        expect.anything(),
      );
    });
  });

  // Task 5 — fire-now result card: onFireResult wired from PlaygroundTab into
  // Transcript → TurnBlock → CompactRow → ToolCallCard's 「立即触发」 button,
  // and the delivered/pending result rendered inline as a 「任务结果」 card
  // with created/fired/completed lifecycle chips (Spec 1 PR4).
  describe("fire-now result card", () => {
    function manageTaskEvents(): SseEvent[] {
      return [
        {
          id: "m",
          event: "metadata",
          data: { run_id: "run-mt-1" },
          rawData: "",
          receivedAt: "t1",
        },
        {
          id: "u1",
          event: "updates",
          data: {
            agent: {
              messages: [
                {
                  type: "ai",
                  content: "",
                  tool_calls: [
                    {
                      id: "c1",
                      name: "manage_task",
                      args: { action: "create", name: "每天早上3点搜AI新闻" },
                      type: "tool_call",
                    },
                  ],
                },
              ],
            },
          },
          rawData: "",
          receivedAt: "t2",
        },
        {
          id: "u2",
          event: "updates",
          data: {
            tools: {
              messages: [
                {
                  type: "tool",
                  tool_call_id: "c1",
                  name: "manage_task",
                  content: "Created trigger trig-1",
                  status: "success",
                  artifact: { trigger_id: "trig-1", action: "create" },
                },
              ],
            },
          },
          rawData: "",
          receivedAt: "t3",
        },
        { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t4" },
      ];
    }

    async function fireFromManageTaskCard(
      user: ReturnType<typeof userEvent.setup>,
    ) {
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(makeStream(manageTaskEvents()));
      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(
        screen.getByTestId("playground-input"),
        "每天早上3点搜AI新闻",
      );
      await user.click(screen.getByTestId("playground-run"));
      // Task 19 — the middle column's compact tool row expands straight to
      // the ToolCallCard (no more Gantt row → step head → card chain).
      // §八.3 — 运行中过程条自动展开,完成后折叠;折叠时先点头部展开。
      if (!screen.queryByTestId("console-process-steps")) {
        await user.click(await screen.findByTestId("console-process-head"));
      }
      await user.click(await screen.findByTestId("console-row-tool"));
      await user.click(await screen.findByTestId("tool-fire-now"));
    }

    it("renders the delivered text and a green completed chip", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-1",
        thread_id: "44444444-4444-4444-4444-444444444444",
        run_status: "completed",
        trigger_run_status: "succeeded",
        delivery: "delivered",
        delivered_text: "今日 AI 新闻:GPT-6 发布。",
      };
      fireTriggerNowMock.mockResolvedValue(result);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      const card = await screen.findByTestId("playground-task-result");
      expect(card).toHaveTextContent("今日 AI 新闻:GPT-6 发布。");
      expect(
        within(card).getByTestId("playground-task-result-completed"),
      ).toHaveClass("ant-tag-success");
    });

    it("renders the pending hint when the result hasn't landed yet", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-2",
        thread_id: "55555555-5555-5555-5555-555555555555",
        run_status: "running",
        trigger_run_status: "fired",
        delivery: "pending",
        delivered_text: null,
      };
      fireTriggerNowMock.mockResolvedValue(result);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      const card = await screen.findByTestId("playground-task-result");
      expect(card).toHaveTextContent(i18n.t("playground.fire_pending"));
      expect(
        within(card).getByTestId("playground-task-result-completed"),
      ).not.toHaveClass("ant-tag-success");
    });

    // Bug fix — ``taskResults`` used to survive a conversation switch: firing a
    // task in thread A left its 「任务结果」 card rendered underneath thread B's
    // (empty) transcript after "New session" / resuming a different thread,
    // making A's result look like it belonged to B.
    it("clears the task-result card when starting a new session", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-3",
        thread_id: "66666666-6666-6666-6666-666666666666",
        run_status: "completed",
        trigger_run_status: "succeeded",
        delivery: "delivered",
        delivered_text: "今日 AI 新闻:GPT-6 发布。",
      };
      fireTriggerNowMock.mockResolvedValue(result);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      await screen.findByTestId("playground-task-result");

      await user.click(screen.getByTestId("playground-new-session"));

      expect(
        screen.queryByTestId("playground-task-result"),
      ).not.toBeInTheDocument();
    });

    it("clears the task-result card when resuming a different thread", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-4",
        thread_id: "77777777-7777-7777-7777-777777777777",
        run_status: "completed",
        trigger_run_status: "succeeded",
        delivery: "delivered",
        delivered_text: "今日 AI 新闻:GPT-6 发布。",
      };
      fireTriggerNowMock.mockResolvedValue(result);
      const past: ThreadMeta = {
        ...sampleThread,
        thread_id: "aaaaaaaa-0000-0000-0000-000000000009",
      };
      listSessionsMock.mockResolvedValue([past]);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      await screen.findByTestId("playground-task-result");

      await resumeFromSidebar(user, past.thread_id);

      expect(
        screen.queryByTestId("playground-task-result"),
      ).not.toBeInTheDocument();
    });

    // PR4 Task 4 (N3) — a fire-now poll timeout returns an empty thread_id
    // (the run hasn't produced a readable thread yet); the 「查看运行」 link
    // must not render a dead /conversations/ link for it.
    it("does not render the view-run link when thread_id is empty (timeout — no run to open yet)", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-timeout",
        thread_id: "",
        run_status: "running",
        trigger_run_status: "fired",
        delivery: "pending",
        delivered_text: null,
      };
      fireTriggerNowMock.mockResolvedValue(result);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      const card = await screen.findByTestId("playground-task-result");
      expect(
        within(card).queryByTestId("playground-task-result-view-run"),
      ).not.toBeInTheDocument();
    });

    it("renders the view-run link when thread_id is non-empty", async () => {
      const user = userEvent.setup();
      const result: FireNowResult = {
        run_id: "run-fired-viewrun",
        thread_id: "99999999-9999-9999-9999-999999999999",
        run_status: "completed",
        trigger_run_status: "succeeded",
        delivery: "delivered",
        delivered_text: "今日 AI 新闻:GPT-6 发布。",
      };
      fireTriggerNowMock.mockResolvedValue(result);

      await fireFromManageTaskCard(user);

      await waitFor(() =>
        expect(fireTriggerNowMock).toHaveBeenCalledWith("trig-1"),
      );
      const card = await screen.findByTestId("playground-task-result");
      expect(
        within(card).getByTestId("playground-task-result-view-run"),
      ).toBeInTheDocument();
    });
  });
});

// Track C W2 — 切入态只读:一期只开读,写操作全置灰(两态断言)。
describe("PlaygroundTab — 切入态只读 (Track C W2)", () => {
  it("切入态:发送/运行与新建会话按钮置灰", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    renderPg();
    const run = await screen.findByTestId("playground-run");
    expect(run).toBeDisabled();
    expect(screen.getByTestId("playground-new-session")).toBeDisabled();
  });

  it("归属态:新建会话可用,输入后发送可用", async () => {
    const user = userEvent.setup();
    renderPg();
    await screen.findByTestId("playground-input");
    expect(screen.getByTestId("playground-new-session")).not.toBeDisabled();
    await user.type(screen.getByTestId("playground-input"), "hi");
    expect(screen.getByTestId("playground-run")).not.toBeDisabled();
  });

  // Cross-tenant W3 — 上传图片/文档也是写操作(会话工作区落盘),两态断言。
  it("切入态:上传图片/文档按钮置灰;归属态可用", async () => {
    const first = renderPg();
    await screen.findByTestId("playground-input");
    expect(screen.getByTestId("playground-attach")).toBeEnabled();
    expect(screen.getByTestId("playground-attach-doc")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderPg();
    await screen.findByTestId("playground-input");
    expect(screen.getByTestId("playground-attach")).toBeDisabled();
    expect(screen.getByTestId("playground-attach-doc")).toBeDisabled();
  });

  // fix-review Minor#2 — 重试按钮切入态用「不渲染」实现(onRetry={undefined}),
  // 两渲染点(live 轮 + resume 历史轮)各补两态断言。切入态在 home 态跑出
  // transcript 后翻 mock + rerender 模拟(顶栏切换器实时可达该状态)。
  const pgTree = tree(sampleDetail);

  it("live 轮:归属态渲染重试按钮,切入态不渲染", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    streamRunMock.mockReturnValue(
      makeStream([
        {
          id: "u",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "an answer" }] } },
          rawData: "",
          receivedAt: "",
        },
        { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
      ]),
    );

    const view = renderPg();
    await screen.findByTestId("playground-input");
    await user.type(screen.getByTestId("playground-input"), "q1");
    await user.click(screen.getByTestId("playground-run"));
    await findInTranscript("an answer");
    expect(screen.getByTestId("playground-turn-retry")).toBeInTheDocument();

    isTenantSwitchedMock.mockReturnValue(true);
    view.rerender(pgTree);
    expect(
      screen.queryByTestId("playground-turn-retry"),
    ).not.toBeInTheDocument();
  });

  it("resume 历史轮:归属态渲染重试按钮,切入态不渲染", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    const past: ThreadMeta = {
      ...sampleThread,
      thread_id: "aaaaaaaa-0000-0000-0000-00000000000a",
    };
    listSessionsMock.mockResolvedValue([past]);
    getMessagesMock.mockResolvedValue([
      { role: "user", content: "q1" },
      { role: "assistant", content: "a1" },
    ]);
    listThreadRunsMock.mockResolvedValue([
      { runId: "r1", status: "success", isResume: false, createdAt: "t1", tokens: null },
    ]);
    streamRunEventsMock.mockReturnValue(
      makeStream([
        {
          id: "u1",
          event: "updates",
          data: {
            agent: { messages: [{ type: "ai", content: "replayed answer" }] },
          },
          rawData: "",
          receivedAt: "t1",
        },
        { id: "e1", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
      ]),
    );

    const view = renderPg();
    await screen.findByTestId("playground-input");
    await resumeFromSidebar(user, past.thread_id);
    await findInTranscript("replayed answer");
    expect(screen.getByTestId("playground-turn-retry")).toBeInTheDocument();

    isTenantSwitchedMock.mockReturnValue(true);
    view.rerender(pgTree);
    expect(
      screen.queryByTestId("playground-turn-retry"),
    ).not.toBeInTheDocument();
  });
});
