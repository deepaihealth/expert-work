/**
 * RowDetail tests — Task 17 of the debug-console PR-A plan. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md
 * for the five required `it`s (verbatim titles below).
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import type { SpanMatch } from "../../../api/trace_match";
import { trajectoryRowsOf, type TrajectoryRow } from "../../../api/trajectory_rows";
import { RowDetail, type RowDetailProps } from "../RowDetail";

// ToolCallCard's fire-now button reads useIsTenantSwitched (Auth/TenantScope
// providers not mounted here) — same stub as ToolTimeline.test.tsx.
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "t" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

const PLAN = {
  goal: "出建议",
  steps: [
    { id: "1", description: "查档案", status: "completed" },
    { id: "2", description: "分析", status: "in_progress" },
    { id: "3", description: "出建议", status: "pending" },
  ],
};
const INPUT = { text: "帮我查一下", attachmentNames: [], inputs: {} };

// Step 1: think (model gpt-x, in 120 / out 30) + one tool call.
// Step 2: update_plan, merged with its tools-node plan snapshot.
// Step 3: a manage_task create call — ToolCallCard's fire-now affordance.
const EVENTS: SseEvent[] = [
  upd("agent", {
    step_count: 1,
    _duration_ms: 900,
    messages: [
      {
        type: "ai",
        content: "",
        response_metadata: { model_name: "gpt-x" },
        usage_metadata: { input_tokens: 120, output_tokens: 30 },
        additional_kwargs: { reasoning_content: "先查客户档案\n再看工单" },
        tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
      },
    ],
  }),
  upd("tools", {
    messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }],
  }),
  upd("agent", {
    step_count: 2,
    messages: [
      {
        type: "ai",
        content: "",
        tool_calls: [
          { id: "p1", name: "update_plan", args: { goal: PLAN.goal, steps: PLAN.steps, reason: "细化" } },
        ],
      },
    ],
  }),
  upd("tools", {
    messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }],
    plan: PLAN,
  }),
  upd("agent", {
    step_count: 3,
    messages: [
      {
        type: "ai",
        content: "",
        tool_calls: [{ id: "mt1", name: "manage_task", args: { action: "create" } }],
      },
    ],
  }),
  upd("tools", {
    messages: [
      {
        type: "tool",
        tool_call_id: "mt1",
        name: "manage_task",
        content: "ok",
        status: "success",
        artifact: { trigger_id: "trg-1", action: "create" },
      },
    ],
  }),
];

const ROWS = trajectoryRowsOf(EVENTS, INPUT, "## Done\n\nAll set.", "done");

function findRow(pred: (r: TrajectoryRow) => boolean): TrajectoryRow {
  const row = ROWS.find(pred);
  if (!row) throw new Error("fixture row not found");
  return row;
}
const thinkRow = findRow((r) => r.kind === "think");
const toolRow = findRow((r) => r.kind === "tool" && r.entry.toolName === "query_crm");
const manageTaskRow = findRow((r) => r.kind === "tool" && r.entry.toolName === "manage_task");
const planRow = findRow((r) => r.kind === "plan");
const userRow = findRow((r) => r.kind === "user");
const assistantRow = findRow((r) => r.kind === "assistant");

const NO_TRACE: SpanMatch = { span: null, reason: "no_trace" };

function renderDetail(row: TrajectoryRow, over: Partial<RowDetailProps> = {}) {
  return render(
    <App>
      <RowDetail
        row={row}
        rowIndex={1}
        turnSeq={0}
        events={EVENTS}
        match={NO_TRACE}
        trace={null}
        traceLoading={false}
        onRefreshTrace={() => {}}
        onClose={() => {}}
        {...over}
      />
    </App>,
  );
}

function clickTab(key: string) {
  fireEvent.click(screen.getByTestId(`console-detail-tab-${key}`));
}

describe("RowDetail", () => {
  it("summary lists level/status/duration and think-specific model + tokens", () => {
    // 第 1 轮 · 第 1 步;模型 gpt-x;入 120 · 出 30
    renderDetail(thinkRow);
    const summary = screen.getByTestId("console-detail-summary");
    expect(within(summary).getByText("Turn 1 · step 1")).toBeInTheDocument();
    expect(within(summary).getByText("done")).toBeInTheDocument();
    expect(within(summary).getByText("900ms")).toBeInTheDocument();
    expect(within(summary).getByText("gpt-x")).toBeInTheDocument();
    expect(within(summary).getByText("In 120 · out 30")).toBeInTheDocument();
  });

  it("payload: tool args JSON with copy; think without span shows the need-langfuse hint", () => {
    renderDetail(toolRow);
    clickTab("payload");
    const toolPayload = screen.getByTestId("console-detail-payload");
    expect(toolPayload.textContent).toContain('"id": "C-1"');
    expect(screen.getByTestId("console-detail-payload-copy")).toBeInTheDocument();
    cleanup();

    renderDetail(thinkRow);
    clickTab("payload");
    expect(screen.getByTestId("console-detail-payload").textContent).toContain(
      "LLM input is only available from the Langfuse trace",
    );
  });

  it("result: tool renders ToolCallCard (fire-now reachable), plan renders steps with glyphs, assistant renders markdown", () => {
    renderDetail(manageTaskRow);
    clickTab("result");
    expect(screen.getByTestId("tool-call-card")).toBeInTheDocument();
    expect(screen.getByTestId("tool-fire-now")).toBeInTheDocument();
    cleanup();

    renderDetail(planRow);
    clickTab("result");
    const planResult = screen.getByTestId("console-detail-result");
    expect(within(planResult).getByText("查档案")).toBeInTheDocument();
    expect(within(planResult).getByText("✓")).toBeInTheDocument();
    expect(within(planResult).getByText("◐")).toBeInTheDocument();
    expect(within(planResult).getByText("○")).toBeInTheDocument();
    cleanup();

    renderDetail(assistantRow);
    clickTab("result");
    expect(screen.getByRole("heading", { level: 2, name: "Done" })).toBeInTheDocument();
  });

  it("raw: one EventCard per eventIndexes entry; empty → no-frames text", () => {
    renderDetail(planRow);
    clickTab("raw");
    expect(planRow.eventIndexes.length).toBeGreaterThan(1);
    expect(screen.getAllByTestId("event-card-updates")).toHaveLength(planRow.eventIndexes.length);
    cleanup();

    renderDetail(userRow);
    clickTab("raw");
    expect(userRow.eventIndexes).toHaveLength(0);
    expect(screen.getByText("No raw frames for this row.")).toBeInTheDocument();
  });

  it("tab stays put when the row changes; close button calls onClose", () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <App>
        <RowDetail
          row={thinkRow}
          rowIndex={1}
          turnSeq={0}
          events={EVENTS}
          match={NO_TRACE}
          trace={null}
          traceLoading={false}
          onRefreshTrace={() => {}}
          onClose={onClose}
        />
      </App>,
    );
    clickTab("timing");
    const isTimingSelected = () =>
      screen.getByTestId("console-detail-tab-timing").closest('[role="tab"]')?.getAttribute("aria-selected");
    expect(isTimingSelected()).toBe("true");

    rerender(
      <App>
        <RowDetail
          row={toolRow}
          rowIndex={1}
          turnSeq={0}
          events={EVENTS}
          match={NO_TRACE}
          trace={null}
          traceLoading={false}
          onRefreshTrace={() => {}}
          onClose={onClose}
        />
      </App>,
    );
    // Still on Timing — no reset just because `row` changed.
    expect(isTimingSelected()).toBe("true");

    fireEvent.click(screen.getByTestId("console-detail-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
  it("header shows #rowIndex + kind (no turn label — the panel header owns it) and the root has its own padding", () => {
    renderDetail(toolRow, { rowIndex: 4 });

    const header = screen.getByTestId("console-detail-header");
    expect(header.textContent).toContain("#4");
    expect(header.textContent).toContain("TOOL");
    // 「第 N 轮」 moved to the panel header (spec §八.8) — the key itself stays
    // for SummaryTab, but the detail header must not repeat it.
    expect(header.textContent).not.toContain("Turn 1");

    const root = header.parentElement as HTMLElement;
    expect(root.style.padding).toBe("8px 12px");
    expect(root.style.height).toBe("100%");
    expect(root.style.overflow).toBe("auto");
  });
});
