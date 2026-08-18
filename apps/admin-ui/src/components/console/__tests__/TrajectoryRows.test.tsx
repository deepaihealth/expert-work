/**
 * TrajectoryRows — the debug console's flat right-rail trajectory row list
 * (调试台重设计 PR-A Task 16). Locale-sensitive assertions below (kind
 * labels / summaries are Chinese) — pin zh-CN explicitly and restore
 * afterward, same pattern as AttachmentChips.test.tsx.
 */
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import { TrajectoryRows, rowSummary, type TrajectoryRowsProps } from "../TrajectoryRows";
import { trajectoryRowsOf, type PlanRow, type TrajectoryInput, type TrajectoryRow } from "../../../api/trajectory_rows";
import type { SseEvent } from "../../../api/sessions";

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "t" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

// Same fixture as api/__tests__/trajectory_rows.test.ts's
// `describe("trajectoryRowsOf")` block — a step with one tool call, a
// second step that carries the final answer.
const INPUT: TrajectoryInput = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };
const EVENTS: SseEvent[] = [
  upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
  upd("tools", { messages: [{ type: "tool", tool_call_id: "a", name: "t1", content: "r", status: "success" }] }),
  upd("agent", { step_count: 2, _duration_ms: 400, messages: [{ type: "ai", content: "最终答案", response_metadata: { model_name: "gpt-x" } }] }),
  ev("end", { status: "success" }),
];

function rowsOf(): TrajectoryRow[] {
  return trajectoryRowsOf(EVENTS, INPUT, "答", "done");
}

function Wrapper(props: Partial<TrajectoryRowsProps> & { rows: readonly TrajectoryRow[] }) {
  const [selectedRowId, setSelectedRowId] = useState<string | null>(props.selectedRowId ?? null);
  return (
    <TrajectoryRows
      rows={props.rows}
      selectedRowId={selectedRowId}
      onSelectRow={props.onSelectRow ?? setSelectedRowId}
      running={props.running ?? false}
    />
  );
}

describe("TrajectoryRows", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders one row per trajectory row with kind label, summary and duration; llm-call summary for empty think", () => {
    const rows = rowsOf();
    render(<TrajectoryRows rows={rows} selectedRowId={null} onSelectRow={vi.fn()} running={false} />);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered).toHaveLength(rows.length);

    // rows: [user, think(step1, text=""), tool, think(step2, text="", model gpt-x), assistant]
    expect(rendered[1]).toHaveAttribute("data-kind", "think");
    expect(rendered[1].textContent).toContain("模型调用");

    const toolRow = rendered.find((el) => el.getAttribute("data-kind") === "tool");
    expect(toolRow?.textContent).toContain("t1");
    expect(toolRow?.textContent).toContain("r");

    expect(rendered.at(-1)).toHaveAttribute("data-kind", "assistant");
  });

  it("clicking a row calls onSelectRow(id); the selected row is aria-selected", async () => {
    const rows = rowsOf();
    const onSelectRow = vi.fn();
    render(<TrajectoryRows rows={rows} selectedRowId={rows[0].id} onSelectRow={onSelectRow} running={false} />);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered[0]).toHaveAttribute("aria-selected", "true");
    expect(rendered[2]).toHaveAttribute("aria-selected", "false");

    await userEvent.click(rendered[2]);
    expect(onSelectRow).toHaveBeenCalledWith(rows[2].id);
  });

  it("ArrowDown/ArrowUp move the selection to the neighbouring row", () => {
    const rows = rowsOf();
    render(<Wrapper rows={rows} selectedRowId={rows[1].id} />);

    const list = screen.getByTestId("console-traj-rows");
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(screen.getAllByTestId("console-traj-row")[2]).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(list, { key: "ArrowUp" });
    fireEvent.keyDown(list, { key: "ArrowUp" });
    expect(screen.getAllByTestId("console-traj-row")[0]).toHaveAttribute("aria-selected", "true");
  });

  it("running rows show the pulse; error rows carry data-status=error", () => {
    // Reuses the compact-rows fixture that produces one pending (running)
    // tool row and one error tool row (api/__tests__/trajectory_rows.test.ts).
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, messages: [{
        type: "ai", content: "",
        tool_calls: [{ id: "a", name: "t1", args: {} }, { id: "b", name: "t2", args: {} }],
      }] }),
      upd("tools", { messages: [
        { type: "tool", tool_call_id: "b", name: "t2", content: "boom", status: "error" },
      ] }),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "running");
    render(<TrajectoryRows rows={rows} selectedRowId={null} onSelectRow={vi.fn()} running />);

    const rendered = screen.getAllByTestId("console-traj-row");
    const runningRow = rendered.find((el) => el.getAttribute("data-status") === "running");
    expect(runningRow?.querySelector(".ew-traj-row__pulse")).toBeInTheDocument();

    const errorRow = rendered.find((el) => el.getAttribute("data-status") === "error");
    expect(errorRow).toBeTruthy();
    expect(errorRow?.querySelector(".ew-traj-row__pulse")).not.toBeInTheDocument();
  });

  it("plan row summary: update_plan appends the reason's first sentence; planner has no suffix", () => {
    const t = i18n.getFixedT("zh-CN");
    const updatePlanRow: PlanRow = {
      id: "plan:1", kind: "plan", seq: 1, step: 1, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null,
      source: "update_plan", callId: "p1", plannerSeq: null,
      stepsTotal: 3, goal: "出建议", reason: "档案查完了,细化后两步。再看工单", plan: null,
    };
    const plannerRow: PlanRow = {
      id: "plan:2", kind: "plan", seq: 2, step: null, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null,
      source: "planner", callId: null, plannerSeq: null,
      stepsTotal: 3, goal: "出建议", reason: null, plan: null,
    };
    expect(rowSummary(updatePlanRow, t)).toBe("计划 · 更新为 3 步 · 档案查完了,细化后两步");
    expect(rowSummary(plannerRow, t)).toBe("制定计划 · 3 步");
  });

  it("plan row summary: same first-sentence rule as the middle column (M1) — `!` cuts, a decimal point doesn't", () => {
    const t = i18n.getFixedT("zh-CN");
    const row: PlanRow = {
      id: "plan:3", kind: "plan", seq: 3, step: 1, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null,
      source: "update_plan", callId: "p3", plannerSeq: null,
      stepsTotal: 3, goal: "出建议", reason: "置信度 0.8 不够!再查一遍", plan: null,
    };
    expect(rowSummary(row, t)).toBe("计划 · 更新为 3 步 · 置信度 0.8 不够");
  });
});
