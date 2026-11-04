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

const { getRunMock } = vi.hoisted(() => ({ getRunMock: vi.fn() }));
vi.mock("../../../api/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/runs")>()),
  getRun: getRunMock,
}));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({ scope: "home", setScope: () => {}, apiTenantScope: undefined }),
}));

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
});

function frame(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
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
    runId: "run-1",
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
  return { run_id: "run-1", thread_id: "th-1", status: "running", pending_approval: null, ...over };
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

describe("AnswerBubble progress notes", () => {
  it("a running turn shows the server's progress line", async () => {
    getRunMock.mockResolvedValue(
      detail({
        progress: {
          elapsed_s: 754,
          step: 12,
          tool_calls: 30,
          active_workers: 2,
          current: { node: "agent", tool: "web_search" },
          last_event_at: null,
        },
      }),
    );
    renderBubble(turnOf([frame("metadata", { run_id: "run-1" })]));

    expect(await screen.findByTestId("console-turn-progress")).toHaveTextContent(
      "已进行 12 分钟 · 第 12 步 · 调用工具 30 次 · 子任务 2 个在跑",
    );
    expect(getRunMock).toHaveBeenCalledWith("th-1", "run-1", undefined);
  });

  it("without a thread id nothing is read and no line shows", () => {
    renderBubble(turnOf([]), null);
    expect(getRunMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("console-turn-progress")).not.toBeInTheDocument();
  });

  it("a live queued turn reads the head's start from the queued frame", () => {
    const events = [frame("queued", { ahead: 1, ahead_started_at: minutesAgo(7) })];
    renderBubble(turnOf(events, { runId: null, queued: true }));

    expect(screen.getByTestId("console-turn-queued")).toHaveTextContent(
      "排队中，上一轮已进行 7 分钟",
    );
    expect(getRunMock).not.toHaveBeenCalled();
  });

  it("a history queued turn reads queued_behind from the run detail", async () => {
    getRunMock.mockResolvedValue(
      detail({
        status: "queued",
        queued_behind: { run_id: "run-0", status: "running", started_at: minutesAgo(3) },
      }),
    );
    renderBubble(turnOf([], { source: "history", queued: true }));

    expect(await screen.findByText("排队中，上一轮已进行 3 分钟")).toBeInTheDocument();
  });
});
