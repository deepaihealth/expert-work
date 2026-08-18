/**
 * TurnBlock — the console's per-turn transcript block (Task 11): user bubble
 * + compact rows (settled + live-synthetic) + approval gate + answer +
 * footer. Fixtures build real SSE ``updates`` frames (``upd()``, same style
 * as ``api/__tests__/timeline.test.ts``) so ``compactRowsOf``/``summarizeTurn``
 * derive the rendered rows/answer for real, instead of hand-rolled row
 * objects.
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { TurnBlock, type TurnBlockProps } from "../TurnBlock";
import type { ConsoleTurn } from "../types";
import type { SseEvent } from "../../../api/sessions";
import type { ApprovalItem } from "../../../api/approvals";
import type { Turn } from "../../turn/types";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

function makeConsoleTurn(
  events: SseEvent[],
  turnOver: Partial<Turn> = {},
): ConsoleTurn {
  return {
    key: "t1",
    seq: 0,
    source: "live",
    turn: {
      id: "t1",
      input: "帮我查一下客户",
      attachments: [],
      events,
      status: "done",
      error: null,
      approval: null,
      ...turnOver,
    },
    runId: "run-1",
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
  };
}

function liveStep(overrides: Partial<LiveStep> = {}): LiveStep {
  return {
    content: "",
    reasoning: "",
    toolNames: new Map(),
    reasoningMs: null,
    ...overrides,
  };
}

function makeBaseProps(turn: ConsoleTurn): TurnBlockProps {
  return {
    turn,
    threadId: "th-1",
    selected: false,
    onSelect: vi.fn(),
    onInspectRow: vi.fn(),
    rate: null,
    isSystemAdmin: false,
    readOnly: false,
    isTenantSwitched: false,
    onDecide: vi.fn(),
    deciding: false,
    onExport: vi.fn(),
    exporting: false,
    onDownloadArtifact: vi.fn().mockResolvedValue(undefined),
  };
}

function renderTurnBlock(props: TurnBlockProps) {
  return render(
    <MemoryRouter>
      <App>
        <TurnBlock {...props} />
      </App>
    </MemoryRouter>,
  );
}

// One agent step with reasoning + a tool call, its result, then a second
// step with the final answer — a fully settled turn.
const settledEvents: SseEvent[] = [
  upd("agent", {
    step_count: 1,
    messages: [
      {
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "先查客户资料" },
        response_metadata: { finish_reason: "tool_calls", model_name: "glm-5.2" },
        usage_metadata: { input_tokens: 100, output_tokens: 10, total_tokens: 110 },
        tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
      },
    ],
  }),
  upd("tools", {
    messages: [
      { type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条客户记录", status: "success" },
    ],
  }),
  upd("agent", {
    step_count: 2,
    messages: [
      {
        type: "ai",
        content: "已查完,客户共 3 条记录",
        response_metadata: { finish_reason: "stop" },
        usage_metadata: { input_tokens: 50, output_tokens: 20, total_tokens: 70 },
      },
    ],
  }),
];

describe("TurnBlock", () => {
  it("① settled turn: user bubble + think row + tool row + answer + footer", () => {
    const turn = makeConsoleTurn(settledEvents, { status: "done" });
    renderTurnBlock(makeBaseProps(turn));

    expect(screen.getByText("帮我查一下客户")).toBeInTheDocument();
    expect(screen.getByTestId("console-row-think")).toHaveTextContent("先查客户资料");
    expect(screen.getByTestId("console-row-tool")).toHaveTextContent("query_crm");
    expect(screen.getByText(/已查完,客户共 3 条记录/)).toBeInTheDocument();
    expect(screen.getByTestId("console-turn-status")).toBeInTheDocument();
    expect(screen.getByTestId("playground-export-json")).toBeInTheDocument();
  });

  it("② live turn: an unsettled step synthesizes a running think + tool row and drives the live answer typewriter", () => {
    // Only step 1 has landed via a settled `updates` frame.
    const events: SseEvent[] = [upd("agent", { step_count: 1, messages: [{ type: "ai", content: "" }] })];
    const turn = makeConsoleTurn(events, { status: "running" });
    const liveByStep = new Map<number, LiveStep>([
      [2, liveStep({ content: "partial", reasoning: "thinking…", toolNames: new Map([[0, "query_crm"]]) })],
    ]);
    renderTurnBlock({ ...makeBaseProps(turn), liveByStep });

    const thinkRow = screen.getByTestId("console-row-think");
    expect(thinkRow).toHaveTextContent("thinking…");
    expect(thinkRow).toHaveAttribute("data-status", "running");
    const toolRow = screen.getByTestId("console-row-tool");
    expect(toolRow).toHaveTextContent("query_crm");
    expect(toolRow).toHaveAttribute("data-status", "running");
    expect(screen.getByTestId("console-answer-live")).toHaveTextContent("partial");
  });

  it("③ a settled step's leftover live buffer does not synthesize a row (only the still-unsettled step does)", () => {
    // Step 1 settled with no reasoning of its own; its liveByStep entry is
    // stale leftover from streaming that must not surface. Step 2 is the
    // genuinely unsettled step.
    const events: SseEvent[] = [upd("agent", { step_count: 1, messages: [{ type: "ai", content: "answer1" }] })];
    const turn = makeConsoleTurn(events, { status: "running" });
    const liveByStep = new Map<number, LiveStep>([
      [1, liveStep({ reasoning: "STALE STEP1 REASONING" })],
      [2, liveStep({ reasoning: "LIVE STEP2 REASONING" })],
    ]);
    renderTurnBlock({ ...makeBaseProps(turn), liveByStep });

    expect(screen.queryByText(/STALE STEP1 REASONING/)).not.toBeInTheDocument();
    const thinkRows = screen.getAllByTestId("console-row-think");
    expect(thinkRows).toHaveLength(1);
    expect(thinkRows[0]).toHaveTextContent("LIVE STEP2 REASONING");
  });

  it("④ a pending approval (not read-only) renders the gate; approving calls onDecide(turnId, approval, \"approve\")", async () => {
    const approval: ApprovalItem = {
      id: "ap1",
      tenant_id: "tenant-1",
      user_id: null,
      run_id: "run-1",
      thread_id: "th-1",
      request_id: "req-1",
      node: "manage_task",
      reason_kind: "high_risk",
      action_summary: "发送邮件",
      proposed_args: {},
      requested_at: "",
      timeout_at: "",
      status: "pending",
      decided_by: null,
      decided_at: null,
    };
    const turn = makeConsoleTurn([], { status: "running", approval });
    const onDecide = vi.fn();
    const { rerender } = renderTurnBlock({ ...makeBaseProps(turn), onDecide });

    expect(screen.getByTestId("playground-approval")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("playground-approval-approve"));
    expect(onDecide).toHaveBeenCalledWith("t1", approval, "approve");

    // readOnly hides the gate entirely.
    rerender(
      <MemoryRouter>
        <App>
          <TurnBlock {...makeBaseProps(turn)} onDecide={onDecide} readOnly />
        </App>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
  });

  it("⑤ the dispatched prompt-variable inputs render as a small line under the bubble", () => {
    const turn = makeConsoleTurn([], { inputs: { city: "上海" } });
    renderTurnBlock(makeBaseProps(turn));
    expect(screen.getByTestId("console-turn-inputs")).toHaveTextContent("city=上海");
  });

  it("⑥ clicking the container background selects the turn", async () => {
    const turn = makeConsoleTurn([]);
    const onSelect = vi.fn();
    renderTurnBlock({ ...makeBaseProps(turn), onSelect });

    await userEvent.click(screen.getByTestId("console-turn"));
    expect(onSelect).toHaveBeenCalledWith("t1");
  });

  it("⑦ a row's inspect button calls onInspectRow(turnKey, rowId); the footer's inspect button calls onSelect(key)", async () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [{ type: "ai", content: "", tool_calls: [{ id: "c1", name: "query_crm", args: {} }] }],
      }),
    ];
    const turn = makeConsoleTurn(events);
    const onInspectRow = vi.fn();
    const onSelect = vi.fn();
    renderTurnBlock({ ...makeBaseProps(turn), onInspectRow, onSelect });

    await userEvent.click(screen.getByTestId("console-row-inspect"));
    expect(onInspectRow).toHaveBeenCalledWith("t1", "tool:0:0");
    expect(onSelect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByTestId("console-turn-inspect"));
    expect(onSelect).toHaveBeenCalledWith("t1");
  });
});
