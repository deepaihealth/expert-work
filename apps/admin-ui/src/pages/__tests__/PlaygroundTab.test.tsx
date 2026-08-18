/**
 * PlaygroundTab tests — Stream H.2 PR 3.
 *
 * Both async paths are mocked: ``createSession`` returns a stubbed
 * thread, ``streamRun`` is an async generator we drive frame-by-frame
 * from the test body. This keeps the network layer out of jsdom.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../i18n";
import i18n from "../../i18n";

import * as approvalsSdk from "../../api/approvals";
import { ApiError, setStoredToken } from "../../api/client";
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
import type { RunTrace } from "../../api/trace_facade";
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
const downloadFileMock = vi.spyOn(workspaceSdk, "downloadUserWorkspaceFile");
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
  // The event-view toggle persists to localStorage (shared across turns);
  // clear it so a prior test's "Exact" selection doesn't leak into the next
  // test's initial render.
  window.localStorage.clear();
  createSessionMock.mockReset();
  streamRunMock.mockReset();
  uploadImageMock.mockReset();
  uploadDocumentMock.mockReset();
  getWorkspaceMock.mockReset();
  getWorkspaceMock.mockResolvedValue({ workspace: null, artifacts: [] });
  getWorkspaceFilesMock.mockReset();
  getWorkspaceFilesMock.mockResolvedValue([]);
  downloadFileMock.mockReset();
  downloadFileMock.mockResolvedValue(undefined);
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
  getRunMock.mockReset();
  getRunTraceMock.mockReset();
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

// The per-turn run-detail link uses react-router <Link>, so every render needs
// a Router context. Batch 4b item 15's Langfuse deep link is system_admin
// gated (useAuth()), so every render also needs an AuthProvider — default to
// a non-admin token since most of these tests don't exercise the Langfuse link.
function renderPg(
  detail: AgentDetailResponse = sampleDetail,
  { admin = false }: { admin?: boolean } = {},
) {
  setStoredToken(jwt(admin ? ["system_admin"] : []));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <PlaygroundTab detail={detail} />
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

// Lazy thread creation — no backend session exists until the first action.
// Tests that need a live thread (a run in the transcript, the session header
// id) establish one by sending a throwaway message. The workspace panel is
// user-scoped, so it loads on mount without a thread (see its own test).
async function establishThread(user: ReturnType<typeof userEvent.setup>) {
  streamRunMock.mockReturnValue(
    makeStream([
      { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
    ]),
  );
  await user.type(await screen.findByTestId("playground-input"), "hi");
  await user.click(screen.getByTestId("playground-run"));
  await screen.findByText(/33333333-3333-3333/);
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

  it("streams events from streamRun and renders them in the log", async () => {
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
          data: { agent: { messages: ["hi"] } },
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
    // The per-turn events view defaults to the tool-call timeline; switch this
    // turn to raw events to assert the individual frames.
    await user.click(await screen.findByText(i18n.t("event_stream.view_raw")));
    await screen.findByTestId("event-card-metadata");
    await screen.findByTestId("event-card-updates");
    await screen.findByTestId("event-card-end");
    expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument();
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

  it("lists artifacts with download/delete and hides dotfiles from files", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: "t1",
        user_id: "u1",
        volume_name: "vol-1",
        size_bytes: 1024,
        size_limit_bytes: 1_048_576,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [
        {
          name: "report.pdf",
          kind: "document",
          latest_version: 1,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    getWorkspaceFilesMock.mockResolvedValue([
      { path: "agent_report.md", size: 2048 },
      { path: ".npm/_cacache/index", size: 99 },
      { path: ".mplconfig/matplotlibrc", size: 10 },
    ]);
    renderPg();
    await establishThread(user);

    // Artifact renders as a list row with download + delete affordances.
    expect(
      await screen.findByTestId("playground-workspace-artifact-download"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("playground-workspace-artifact-delete"),
    ).toBeInTheDocument();
    expect(screen.getByText("report.pdf")).toBeInTheDocument();

    // Only the agent's own file shows; the dotfiles (.npm/.mplconfig) are hidden.
    const fileRows = screen.getAllByTestId("playground-workspace-file");
    expect(fileRows).toHaveLength(1);
    expect(screen.getByText("agent_report.md")).toBeInTheDocument();
    expect(screen.queryByText(".npm/_cacache/index")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("playground-workspace-file-delete"),
    ).toBeInTheDocument();
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
    await screen.findByText("first answer");

    await user.type(screen.getByTestId("playground-input"), "q2");
    await user.click(screen.getByTestId("playground-run"));
    await screen.findByText("second answer");

    // Both turns persist (not wiped) + usage chips render per turn.
    expect(screen.getAllByTestId("playground-turn")).toHaveLength(2);
    expect(screen.getAllByTestId("playground-usage")).toHaveLength(2);
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
    await screen.findByText("first answer");

    await user.click(screen.getByTestId("playground-turn-retry"));
    await screen.findByText("retried answer");

    // Re-dispatched to the same thread with the exact same request body.
    expect(streamRunMock).toHaveBeenCalledTimes(2);
    expect(streamRunMock.mock.calls[1][0]).toBe(sampleThread.thread_id);
    expect(streamRunMock.mock.calls[1][1]).toEqual(
      streamRunMock.mock.calls[0][1],
    );
    // The retry appended a new turn — the original stays in the transcript.
    expect(screen.getAllByTestId("playground-turn")).toHaveLength(2);
  });

  // Cross-tenant W4(D2)— rate_card 是 system_admin-only:非 admin 挂载
  // 调试台不再发 listRateCards(否则每次都吃一发静默 403)。
  it("does not fetch rate cards for a non-admin user", async () => {
    const costDetail: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        // 真实 API 形状:record.spec 是完整 manifest,不是内层 spec(见 :660 的守形状用例)。
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

  it("shows per-turn cost + step + a run-detail link", async () => {
    const user = userEvent.setup();
    const costDetail: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        // 真实 API 形状:record.spec 是完整 manifest,不是内层 spec(见 :660 的守形状用例)。
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
    await screen.findByText("hi");

    expect(screen.getByTestId("playground-turn-cost")).toBeInTheDocument();
    expect(screen.getByTestId("playground-turn-meta")).toHaveTextContent("2");
    expect(screen.getByTestId("playground-turn-run-link")).toHaveAttribute(
      "href",
      `/runs/${sampleThread.thread_id}/run-77`,
    );
  });

  it("lists past sessions for resume and shows a resumed banner", async () => {
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

    await user.click(screen.getByTestId("playground-history-open"));
    await user.click(
      await screen.findByTestId(`session-history-item-${past.thread_id}`),
    );
    expect(
      await screen.findByTestId("playground-resumed-notice"),
    ).toBeInTheDocument();
    // Prior conversation loaded from the checkpoint and rendered read-only.
    const hist = await screen.findByTestId("playground-history");
    expect(hist).toHaveTextContent("earlier question");
    expect(hist).toHaveTextContent("earlier answer");
    // Home state — the ambient scope resolves to no tenant override.
    expect(getMessagesMock).toHaveBeenCalledWith(past.thread_id, undefined);
  });

  it("shows the workspace inspector with the volume + artifacts", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: sampleThread.tenant_id,
        user_id: "u-1",
        volume_name: "expert-work-ws-t-u",
        size_bytes: 2048,
        size_limit_bytes: 1000000,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [
        {
          name: "report.md",
          kind: "document",
          latest_version: 2,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    renderPg();
    await establishThread(user);
    const panel = await screen.findByTestId("playground-workspace");
    expect(panel).toHaveTextContent("expert-work-ws-t-u");
    expect(panel).toHaveTextContent("2.0 KB");
    expect(panel).toHaveTextContent("report.md");
  });

  it("shows 'no workspace' when the user has none (read-only null)", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    renderPg();
    await establishThread(user);
    expect(
      await screen.findByTestId("playground-workspace-none"),
    ).toBeInTheDocument();
  });

  it("lists workspace files and downloads one on click", async () => {
    const user = userEvent.setup();
    createSessionMock.mockResolvedValue(sampleThread);
    getWorkspaceFilesMock.mockResolvedValue([
      { path: "report.pdf", size: 2048 },
    ]);
    renderPg();
    await establishThread(user);
    const files = await screen.findByTestId("playground-workspace-files");
    expect(files).toHaveTextContent("report.pdf");
    await user.click(
      await screen.findByTestId("playground-workspace-file-download"),
    );
    await waitFor(() =>
      // W3 — trailing (userId, tenantScope) both undefined in the home state.
      expect(downloadFileMock).toHaveBeenCalledWith("report.pdf", undefined, undefined),
    );
  });

  it("loads the workspace inspector without a thread (user-scoped)", async () => {
    // The whole point of the user-scoped route: the panel shows the current
    // user's workspace on mount, before (and after) any session exists — so it
    // survives session deletion. No establishThread() here.
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
    const panel = await screen.findByTestId("playground-workspace");
    expect(panel).toHaveTextContent("expert-work-ws-t-u");
    expect(panel).toHaveTextContent("out.txt");
    // ...and the load was keyed on the caller, not a thread id. W3 —
    // trailing (userId, tenantScope) both undefined in the home state.
    expect(getWorkspaceMock).toHaveBeenCalledWith(undefined, undefined);
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
    await screen.findByText("done after approval");
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

  // Task 11 — timeline view's RunStatusBanner, derived from this turn's own
  // SSE-parsed items (not Langfuse level, unlike the exact view's banner).
  describe("timeline view RunStatusBanner", () => {
    it("shows the ok banner (default timeline view) when no step/marker errored", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          { id: "1", event: "metadata", data: { run_id: "run-tl-ok" }, rawData: "", receivedAt: "" },
          {
            id: "2",
            event: "updates",
            data: { agent: { messages: [{ type: "ai", content: "hi" }] } },
            rawData: "",
            receivedAt: "",
          },
          { id: "3", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
        ]),
      );
      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const banner = await screen.findByTestId("run-status-banner");
      expect(banner).toHaveTextContent(i18n.t("playground.rb_ok"));
      expect(screen.queryByTestId("run-status-jump")).not.toBeInTheDocument();
    });

    it("shows the error banner when a tool call failed, and 'jump' scrolls the failing Gantt row into view", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          { id: "1", event: "metadata", data: { run_id: "run-tl-err" }, rawData: "", receivedAt: "" },
          {
            id: "2",
            event: "updates",
            data: {
              agent: {
                step_count: 3,
                messages: [
                  {
                    type: "ai",
                    content: "",
                    tool_calls: [
                      { id: "c1", name: "exec_python", args: { code: "1/0" }, type: "tool_call" },
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
                    name: null,
                    content: "ZeroDivisionError",
                    status: "error",
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
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const banner = await screen.findByTestId("run-status-banner");
      expect(banner).toHaveTextContent(i18n.t("playground.tl_step", { n: 3 }));

      // I1 — restored: `GanttTimeline` rows now carry `data-error` (see
      // gantt_timeline.ts's `hasError`), so the "jump to error" button
      // renders again and targets the first errored row (same pattern as
      // the "exact" view's trace-row jump test below).
      const errorRow = screen
        .getAllByTestId(/^gantt-row-/)
        .find((row) => row.getAttribute("data-error") === "true");
      expect(errorRow).toBeTruthy();
      const scrollSpy = vi.fn();
      if (errorRow) errorRow.scrollIntoView = scrollSpy;

      await user.click(screen.getByTestId("run-status-jump"));
      expect(scrollSpy).toHaveBeenCalledTimes(1);
    });

    it("shows a top-level error event's message once (not duplicated) when no failing tool step owns the label", async () => {
      // Marker-only failure path: an SSE `error` event with no failed tool
      // call → timelineBannerModel has errorText but errorStepCount === null,
      // so errorText owns the label. It must NOT also render as the message.
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          { id: "1", event: "metadata", data: { run_id: "run-tl-marker" }, rawData: "", receivedAt: "" },
          { id: "2", event: "error", data: { message: "运行崩溃了" }, rawData: "", receivedAt: "" },
          { id: "3", event: "end", data: "ok", rawData: "ok", receivedAt: "" },
        ]),
      );
      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const banner = await screen.findByTestId("run-status-banner");
      const occurrences = (banner.textContent?.match(/运行崩溃了/g) ?? []).length;
      expect(occurrences).toBe(1);
    });

    // Task 3 dropped this test: the timeline eventView's type/query filter
    // (`TimelineFilterBar`) went away with the `StepTimeline` → `GanttTimeline`
    // swap (Gantt has no equivalent row filter), so there is no longer a
    // filter that could hide the failing step out from under the banner. The
    // banner's derivation from the unfiltered `timeline` (not a filtered
    // view) is still pinned by "shows the error banner when a tool call
    // failed" above — this test's premise (a filter to bypass) no longer
    // exists.
  });

  // Batch 4b Task 5 — third "Exact" event-view tier (item 14) + purpose
  // labelling (A') + system_admin-gated Langfuse deep link (item 15).
  describe("exact trace view + Langfuse link", () => {
    it("does not fetch the trace until 'Exact' is selected, then labels the sole llm span primary reasoning (1:1 with agent steps)", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-exact-1" },
            rawData: "",
            receivedAt: "t1",
          },
          {
            id: "u",
            event: "updates",
            data: { agent: { messages: [{ type: "ai", content: "hi" }] } },
            rawData: "",
            receivedAt: "t2",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
        ]),
      );
      getRunTraceMock.mockResolvedValue({
        status: "ok",
        trace: { name: "trace-1", latencyMs: 1000, totalCostUsd: null, spanCount: 1 },
        spans: [
          {
            id: "s1",
            parentId: null,
            kind: "llm",
            label: "LLM call",
            detail: null,
            startMs: 0,
            latencyMs: 500,
            model: "glm-4.6",
            inputTokens: 10,
            outputTokens: 20,
            costUsd: null,
            input: null,
            output: null,
            level: "default",
            statusMessage: null,
            purpose: "",
            group: null,
          },
        ],
      });

      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      // Segmented now has three tiers; still defaults to the tool-call
      // timeline, and the trace endpoint isn't hit before "Exact" is picked.
      const toggle = screen.getByTestId("playground-event-view-toggle");
      expect(within(toggle).getByText(i18n.t("event_stream.view_exact"))).toBeInTheDocument();
      expect(getRunTraceMock).not.toHaveBeenCalled();

      await user.click(within(toggle).getByText(i18n.t("event_stream.view_exact")));

      await waitFor(() =>
        expect(getRunTraceMock).toHaveBeenCalledWith(
          sampleThread.thread_id,
          "run-exact-1",
          undefined,
        ),
      );
      await screen.findByTestId("trace-view");
      expect(screen.getByText(/Primary reasoning/)).toBeInTheDocument();
      // Task 10 — RunStatusBanner renders above the trace, ok state.
      expect(screen.getByTestId("run-status-banner")).toBeInTheDocument();
      expect(screen.queryByTestId("run-status-jump")).not.toBeInTheDocument();
    });

    // Task 10 — RunStatusBanner error state + jump-to-error scroll.
    it("shows the RunStatusBanner in error state when a span errored, and 'jump' scrolls the red-marked row into view", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-exact-err" },
            rawData: "",
            receivedAt: "t1",
          },
          {
            id: "u",
            event: "updates",
            data: { agent: { messages: [{ type: "ai", content: "hi" }] } },
            rawData: "",
            receivedAt: "t2",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
        ]),
      );
      getRunTraceMock.mockResolvedValue({
        status: "ok",
        trace: { name: "trace-1", latencyMs: 1000, totalCostUsd: 0.0021, spanCount: 2 },
        spans: [
          {
            id: "s1",
            parentId: null,
            kind: "session",
            label: "Session run",
            detail: null,
            startMs: 0,
            latencyMs: 1000,
            model: null,
            inputTokens: null,
            outputTokens: null,
            costUsd: null,
            input: null,
            output: null,
            level: "default",
            statusMessage: null,
            purpose: "",
            group: null,
          },
          {
            id: "s2",
            parentId: "s1",
            kind: "tool",
            label: "Tool call",
            detail: "exec_python",
            startMs: 100,
            latencyMs: 500,
            model: null,
            inputTokens: null,
            outputTokens: null,
            costUsd: null,
            input: null,
            output: null,
            level: "error",
            statusMessage: "SandboxTimeout",
            purpose: "",
            group: null,
          },
        ],
      });

      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const toggle = screen.getByTestId("playground-event-view-toggle");
      await user.click(within(toggle).getByText(i18n.t("event_stream.view_exact")));

      await screen.findByTestId("trace-view");
      const banner = screen.getByTestId("run-status-banner");
      expect(banner).toHaveTextContent("Failed at Tool call · exec_python");
      expect(screen.getByText("SandboxTimeout")).toBeInTheDocument();

      const errorRow = screen
        .getAllByTestId("trace-row")
        .find((row) => row.getAttribute("data-error") === "true");
      expect(errorRow).toBeTruthy();
      const scrollSpy = vi.fn();
      if (errorRow) errorRow.scrollIntoView = scrollSpy;

      await user.click(screen.getByTestId("run-status-jump"));
      expect(scrollSpy).toHaveBeenCalledTimes(1);
    });

    it("auto-polls a not_ready trace until it resolves, without a manual refresh", async () => {
      // Real timers: the poll uses a 1500ms setTimeout; fake timers deadlock
      // with userEvent + RTL's waitFor here. A ~2s real wait is reliable.
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-poll-1" },
            rawData: "",
            receivedAt: "t1",
          },
          {
            id: "u",
            event: "updates",
            data: { agent: { messages: [{ type: "ai", content: "hi" }] } },
            rawData: "",
            receivedAt: "t2",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t3" },
        ]),
      );
      // Ingestion window: first fetch is still not_ready, the retry resolves.
      getRunTraceMock.mockResolvedValueOnce({ status: "not_ready" });
      getRunTraceMock.mockResolvedValue({
        status: "ok",
        trace: { name: "trace-1", latencyMs: 1000, totalCostUsd: null, spanCount: 1 },
        spans: [
          {
            id: "s1",
            parentId: null,
            kind: "llm",
            label: "LLM call",
            detail: null,
            startMs: 0,
            latencyMs: 500,
            model: "glm-4.6",
            inputTokens: 10,
            outputTokens: 20,
            costUsd: null,
            input: null,
            output: null,
            level: "default",
            statusMessage: null,
            purpose: "",
            group: null,
          },
        ],
      });

      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const toggle = screen.getByTestId("playground-event-view-toggle");
      await user.click(within(toggle).getByText(i18n.t("event_stream.view_exact")));

      // First fetch → not_ready card (refreshable), no waterfall yet.
      await waitFor(() => expect(getRunTraceMock).toHaveBeenCalledTimes(1));
      expect(screen.getByTestId("trace-refresh")).toBeInTheDocument();
      expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);

      // The 1500ms poll auto-refetches — no user action — and the waterfall
      // renders once the retry returns spans.
      await waitFor(() => expect(getRunTraceMock).toHaveBeenCalledTimes(2), {
        timeout: 3000,
      });
      await waitFor(() =>
        expect(screen.getAllByTestId("trace-row").length).toBeGreaterThan(0),
      );
    }, 10000);

    it("shows a loading state while the exact trace fetch is in flight", async () => {
      const user = userEvent.setup();
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-loading" },
            rawData: "",
            receivedAt: "t1",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
        ]),
      );
      let resolveTrace: (value: RunTrace) => void = () => {};
      getRunTraceMock.mockReturnValue(
        new Promise((resolve) => {
          resolveTrace = resolve;
        }),
      );

      renderPg();
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      await user.click(
        within(screen.getByTestId("playground-event-view-toggle")).getByText(
          i18n.t("event_stream.view_exact"),
        ),
      );
      expect(
        await screen.findByTestId("playground-trace-loading"),
      ).toBeInTheDocument();

      resolveTrace({ status: "no_trace" });
      await screen.findByTestId("trace-view");
      expect(
        screen.queryByTestId("playground-trace-loading"),
      ).not.toBeInTheDocument();
    });

    it("hides the Langfuse link for a non-admin turn even when the run has a trace_id and the base url is configured", async () => {
      const user = userEvent.setup();
      vi.stubEnv("VITE_LANGFUSE_BASE_URL", "https://langfuse.example.com/");
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-nolink" },
            rawData: "",
            receivedAt: "t1",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
        ]),
      );

      renderPg(sampleDetail, { admin: false });
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      // Give any (incorrect) fetch a tick to land before asserting absence.
      await waitFor(() => expect(screen.getByTestId("playground-turn")).toBeInTheDocument());
      expect(screen.queryByTestId("playground-turn-langfuse")).not.toBeInTheDocument();
      expect(getRunMock).not.toHaveBeenCalled();
    });

    it("shows a direct Langfuse link for a system_admin when the run has a trace_id", async () => {
      const user = userEvent.setup();
      vi.stubEnv("VITE_LANGFUSE_BASE_URL", "https://langfuse.example.com/");
      createSessionMock.mockResolvedValue(sampleThread);
      streamRunMock.mockReturnValue(
        makeStream([
          {
            id: "m",
            event: "metadata",
            data: { run_id: "run-link" },
            rawData: "",
            receivedAt: "t1",
          },
          { id: "e", event: "end", data: "ok", rawData: "ok", receivedAt: "t2" },
        ]),
      );
      getRunMock.mockResolvedValue({
        run_id: "run-link",
        thread_id: sampleThread.thread_id,
        status: "success",
        pending_approval: null,
        trace_id: "tr-xyz",
      });

      renderPg(sampleDetail, { admin: true });
      await screen.findByTestId("playground-input");
      await user.type(screen.getByTestId("playground-input"), "hello");
      await user.click(screen.getByTestId("playground-run"));
      await screen.findByTestId("playground-turn");

      const link = await screen.findByTestId("playground-turn-langfuse");
      expect(link).toHaveAttribute(
        "href",
        "https://langfuse.example.com/trace/tr-xyz",
      );
      expect(getRunMock).toHaveBeenCalledWith(sampleThread.thread_id, "run-link", undefined);
    });
  });

  // Task 5 — resume reconstructs history as lazy read-only TurnCards when the
  // message/run counts line up (buildHistoryTurns pairs 1:1); a mismatch or a
  // failed lookup/replay must degrade — never a crash, never lost content.
  describe("history lazy rebuild on resume", () => {
    it("replays a count-matched history run into a full TurnCard when its row scrolls into view", async () => {
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
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );

      await waitFor(() =>
        expect(streamRunEventsMock).toHaveBeenCalledWith(
          past.thread_id,
          "r1",
          expect.anything(),
        ),
      );

      // The replayed answer renders (not just the flat fallback text) — the
      // debug panels filled in from the replayed events.
      await screen.findByText("replayed answer");
      expect(
        screen.queryByText(i18n.t("playground.history_loading")),
      ).not.toBeInTheDocument();
      // Read-only: no mutating control on a finished historical run.
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
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );
      // The retry button only appears once the row's replay settles.
      await screen.findByText("replayed answer");

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
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );

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
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );

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
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );

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

      // Resume A (its message fetch stays pending).
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${threadA.thread_id}`),
      );

      // Resume B before A resolved — B supersedes A (new AbortController).
      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${threadB.thread_id}`),
      );

      // Resolve B first → its history builds + its run replays.
      await act(async () => {
        msgsB.resolve([
          { role: "user", content: "qB" },
          { role: "assistant", content: "aB" },
        ]);
      });
      await screen.findByText("answer-B");

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
      expect(screen.getByText("answer-B")).toBeInTheDocument();
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
  // StepTimeline → AgentStepCard → ToolCallCard's 「立即触发」 button, and the
  // delivered/pending result rendered inline as a 「任务结果」 card with
  // created/fired/completed lifecycle chips (Spec 1 PR4).
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
      // Task 3 — the timeline eventView renders `GanttTimeline`; open the
      // row for this step (its detail reuses `StepTimeline`, which starts
      // collapsed — no error — so a further click reaches the nested
      // ToolCallCard's 「立即触发」 button).
      await user.click(await screen.findByTestId("gantt-row-item-0"));
      await user.click(await screen.findByTestId("step-head"));
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

      await user.click(screen.getByTestId("playground-history-open"));
      await user.click(
        await screen.findByTestId(`session-history-item-${past.thread_id}`),
      );

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
  const pgTree = (
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <PlaygroundTab detail={sampleDetail} />
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>
  );

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
    await screen.findByText("an answer");
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
    await user.click(screen.getByTestId("playground-history-open"));
    await user.click(
      await screen.findByTestId(`session-history-item-${past.thread_id}`),
    );
    await screen.findByText("replayed answer");
    expect(screen.getByTestId("playground-turn-retry")).toBeInTheDocument();

    isTenantSwitchedMock.mockReturnValue(true);
    view.rerender(pgTree);
    expect(
      screen.queryByTestId("playground-turn-retry"),
    ).not.toBeInTheDocument();
  });
});
