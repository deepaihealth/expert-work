/** B-269 / B-270 —— 答案气泡里的排队 / 运行中进度文案(数据来自 run 详情与排队帧)。 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import i18n from "../../../i18n";

import { AnswerBubble } from "../AnswerBubble";
import type { ConsoleTurn } from "../types";
import type { SseEvent } from "../../../api/sessions";
import type { RunDetail } from "../../../api/runs";
import { summarizeTurn } from "../../../api/turn_summary";
import type { Turn } from "../../turn/types";

const { getRunMock, scopeRef } = vi.hoisted(() => ({
  getRunMock: vi.fn(),
  scopeRef: { current: undefined as string | undefined },
}));
vi.mock("../../../api/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/runs")>()),
  getRun: getRunMock,
}));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

let priorLang: string;
beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(priorLang);
});
beforeEach(() => {
  getRunMock.mockReset();
  scopeRef.current = undefined;
  setVisibility("visible");
});

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
}

function frame(event: string, data: unknown, id: string | null = null): SseEvent {
  return { id, event, data, rawData: "", receivedAt: "" };
}

function turnOf(events: SseEvent[], over: Partial<ConsoleTurn> = {}): ConsoleTurn {
  const turn: Turn = {
    id: "t1",
    input: "hi",
    attachments: [],
    events,
    status: "running",
    error: null,
    approval: null,
  };
  return {
    key: "t1",
    seq: 0,
    source: "live",
    turn,
    runId: null,
    loadState: "done",
    fallbackLines: [],
    platformLines: [],
    tokens: null,
    timing: null,
    createdAt: null,
    finishedAt: null,
    runError: null,
    supersededBy: null,
    tombstone: false,
    ...over,
  };
}

function detail(over: Partial<RunDetail>): RunDetail {
  return { run_id: "run-1", thread_id: "th-1", status: "queued", pending_approval: null, ...over };
}

function renderBubble(turn: ConsoleTurn, threadId: string | null = "th-1") {
  render(
    <App>
      <AnswerBubble
        turn={turn}
        summary={summarizeTurn(turn.turn.events)}
        onDownloadArtifact={vi.fn()}
        threadId={threadId}
      />
    </App>,
  );
}

const minutesAgo = (n: number) => new Date(Date.now() - n * 60_000 - 5_000).toISOString();
const queuedFrame = (startedAt: string | null, runId?: string) =>
  frame("queued", { ahead: 1, ahead_started_at: startedAt, ...(runId ? { run_id: runId } : {}) });

describe("AnswerBubble progress notes", () => {
  it("a running turn shows progress counted from its own frames, without a run read", () => {
    const started = Date.now() - 12 * 60_000 - 5_000;
    const tool = { type: "tool", name: "web_search", content: "x" };
    const events = [
      frame("metadata", { run_id: "run-1" }, `${started}-0`),
      frame("updates", { agent: { messages: [], step_count: 12 } }),
      frame("updates", { tools: { messages: [tool, tool] } }),
      frame("worker", { worker_id: "w1", kind: "start" }),
    ];
    renderBubble(turnOf(events, { runId: "run-1" }));

    expect(screen.getByTestId("console-turn-progress")).toHaveTextContent(
      "已进行 12 分钟 · 第 12 步 · 调用工具 2 次 · 子任务 1 个在跑",
    );
    expect(getRunMock).not.toHaveBeenCalled();
  });

  it("a live queued turn without a run id shows the queued frame's snapshot", () => {
    renderBubble(turnOf([queuedFrame(minutesAgo(7))], { queued: true }));

    expect(screen.getByTestId("console-turn-queued")).toHaveTextContent(
      "排队中，上一轮已进行 7 分钟",
    );
    expect(getRunMock).not.toHaveBeenCalled();
  });

  it("a live queued turn refreshes the snapshot from the run detail", async () => {
    getRunMock.mockResolvedValue(
      detail({ queued_behind: { run_id: "run-0", status: "running", started_at: minutesAgo(2) } }),
    );
    renderBubble(turnOf([queuedFrame(minutesAgo(9), "run-1")], { queued: true }));

    expect(await screen.findByText("排队中，上一轮已进行 2 分钟")).toBeInTheDocument();
    expect(getRunMock).toHaveBeenCalledWith("th-1", "run-1");
  });

  it("a history queued turn reads queued_behind from the run detail", async () => {
    getRunMock.mockResolvedValue(
      detail({ queued_behind: { run_id: "run-0", status: "running", started_at: minutesAgo(3) } }),
    );
    renderBubble(turnOf([], { source: "history", runId: "run-1", queued: true }));

    expect(await screen.findByText("排队中，上一轮已进行 3 分钟")).toBeInTheDocument();
  });

  it("does not poll in a switched-tenant view (each read would write a tenant-switch audit row)", () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    renderBubble(turnOf([queuedFrame(minutesAgo(4), "run-1")], { queued: true }));

    expect(screen.getByText("排队中，上一轮已进行 4 分钟")).toBeInTheDocument();
    expect(getRunMock).not.toHaveBeenCalled();
  });

  it("does not poll while the page is hidden", () => {
    setVisibility("hidden");
    renderBubble(turnOf([], { source: "history", runId: "run-1", queued: true }));

    expect(screen.getByText("排队中，上一轮结束后开始")).toBeInTheDocument();
    expect(getRunMock).not.toHaveBeenCalled();
  });
});
