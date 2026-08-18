/**
 * TrajectoryRows — the debug console's flat right-rail trajectory row list
 * (调试台重设计 PR-A Task 16). Locale-sensitive assertions below (kind
 * labels / summaries are Chinese) — pin zh-CN explicitly and restore
 * afterward, same pattern as AttachmentChips.test.tsx.
 */
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
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

/** Fills the props Task 6 made required (hover / range) with neutral values. */
function fullProps(over: Partial<TrajectoryRowsProps> & { rows: readonly TrajectoryRow[] }): TrajectoryRowsProps {
  return {
    selectedRowId: null,
    hoveredRowId: null,
    onHoverRow: vi.fn(),
    onSelectRow: vi.fn(),
    running: false,
    range: null,
    onClearRange: vi.fn(),
    ...over,
  };
}

function Wrapper(props: Partial<TrajectoryRowsProps> & { rows: readonly TrajectoryRow[] }) {
  const [selectedRowId, setSelectedRowId] = useState<string | null>(props.selectedRowId ?? null);
  return (
    <TrajectoryRows
      {...fullProps(props)}
      selectedRowId={selectedRowId}
      onSelectRow={props.onSelectRow ?? setSelectedRowId}
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
    render(<TrajectoryRows {...fullProps({ rows })} />);

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
    render(<TrajectoryRows {...fullProps({ rows, selectedRowId: rows[0].id, onSelectRow })} />);

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
    render(<TrajectoryRows {...fullProps({ rows, running: true })} />);

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
  it("renders the sticky header row with all seven column labels in order", () => {
    render(<TrajectoryRows {...fullProps({ rows: rowsOf() })} />);

    const head = screen.getByTestId("console-traj-head");
    expect(Array.from(head.children).map((c) => c.textContent)).toEqual([
      "#", "类型", "摘要", "入", "出", "思考", "耗时",
    ]);
  });

  it("token columns are filled for think rows only, and data-index counts from 1", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, messages: [{
        type: "ai", content: "",
        additional_kwargs: { reasoning_content: "先想想" },
        usage_metadata: {
          input_tokens: 16000, output_tokens: 900, total_tokens: 16900,
          output_token_details: { reasoning: 770 },
          input_token_details: { cache_read: 14336 },
        },
        tool_calls: [{ id: "a", name: "t1", args: {} }],
      }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "a", name: "t1", content: "r", status: "success" }] }),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "done");
    render(<TrajectoryRows {...fullProps({ rows })} />);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered.map((el) => el.getAttribute("data-index"))).toEqual(
      rows.map((_, i) => String(i + 1)),
    );

    const tokensOf = (el: HTMLElement) =>
      Array.from(el.querySelectorAll(".ew-traj-row__tok")).map((n) => n.textContent);
    const think = rendered.find((el) => el.dataset.kind === "think");
    const tool = rendered.find((el) => el.dataset.kind === "tool");
    expect(tokensOf(think as HTMLElement)).toEqual(["16.0k", "900", "770"]);
    expect(tokensOf(tool as HTMLElement)).toEqual(["", "", ""]);
  });

  it("range renders only the rows inside the 1-based closed span and shows the filter chip", () => {
    const rows = rowsOf();
    expect(rows).toHaveLength(5);
    render(<TrajectoryRows {...fullProps({ rows, range: { from: 2, to: 4 } })} />);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered).toHaveLength(3);
    expect(rendered.map((el) => el.getAttribute("data-index"))).toEqual(["2", "3", "4"]);

    const chip = screen.getByTestId("console-traj-filter");
    expect(chip.textContent).toContain("#2–#4");
    expect(chip.textContent).toContain("3");
  });

  it("no chip without a range; the chip's ✕ calls onClearRange", async () => {
    const rows = rowsOf();
    const onClearRange = vi.fn();
    const { rerender } = render(<TrajectoryRows {...fullProps({ rows })} />);
    expect(screen.queryByTestId("console-traj-filter")).not.toBeInTheDocument();

    rerender(<TrajectoryRows {...fullProps({ rows, range: { from: 2, to: 4 }, onClearRange })} />);
    const chip = screen.getByTestId("console-traj-filter");
    await userEvent.click(within(chip).getByRole("button"));
    expect(onClearRange).toHaveBeenCalledTimes(1);
  });

  it("hovering a row calls onHoverRow(id) then null on leave; hoveredRowId marks the row data-hovered", () => {
    const rows = rowsOf();
    const onHoverRow = vi.fn();
    const { rerender } = render(<TrajectoryRows {...fullProps({ rows, onHoverRow })} />);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered[2]).not.toHaveAttribute("data-hovered");

    fireEvent.mouseOver(rendered[2]);
    expect(onHoverRow).toHaveBeenCalledWith(rows[2].id);
    fireEvent.mouseOut(rendered[2]);
    expect(onHoverRow).toHaveBeenLastCalledWith(null);

    rerender(<TrajectoryRows {...fullProps({ rows, hoveredRowId: rows[2].id })} />);
    const after = screen.getAllByTestId("console-traj-row");
    expect(after[2]).toHaveAttribute("data-hovered", "true");
    expect(after[1]).not.toHaveAttribute("data-hovered");
  });
});
