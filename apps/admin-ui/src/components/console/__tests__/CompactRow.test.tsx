/**
 * CompactRow — the console's middle-column step row (Task 10). Covers the
 * one-line summary per row kind, the expandable/marker split, and the
 * inspect button's opt-in rendering.
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { CompactRow, rowIsExpandable } from "../CompactRow";
import type {
  MarkerRow,
  MemoryRow,
  PlanRow,
  ReflectRow,
  SubagentRow,
  ThinkRow,
  ToolRow,
} from "../../../api/trajectory_rows";

describe("CompactRow", () => {
  it("tool row: name · args → result first line; click expands the ToolCallCard", async () => {
    const row: ToolRow = {
      id: "tool:0:0",
      kind: "tool",
      seq: 0,
      step: 1,
      status: "ok",
      durationMs: 420,
      eventIndexes: [0, 1],
      serverMs: null,
      entry: {
        id: "c1",
        rawName: "query_crm",
        isMcp: false,
        server: null,
        toolName: "query_crm",
        args: { id: "C-1" },
        status: "success",
        resultPreview: "3 条记录\n第二行",
        durationMs: 420,
      },
    };
    const onToggle = vi.fn();
    const { rerender } = render(
      <App>
        <CompactRow row={row} expanded={false} onToggle={onToggle} />
      </App>,
    );
    expect(screen.getByTestId("console-row-tool")).toHaveTextContent(
      /query_crm.*"id":"C-1".*3 条记录/,
    );
    expect(screen.getByTestId("console-row-tool")).not.toHaveTextContent("第二行");
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    expect(onToggle).toHaveBeenCalled();
    rerender(
      <App>
        <CompactRow row={row} expanded onToggle={onToggle} />
      </App>,
    );
    expect(
      within(screen.getByTestId("console-row-detail")).getByTestId("tool-call-card"),
    ).toBeInTheDocument();
  });

  it("think row shows the first line settled and the latest line while live", () => {
    const row: ThinkRow = {
      id: "think:0",
      kind: "think",
      seq: 0,
      step: 1,
      status: "ok",
      durationMs: 800,
      eventIndexes: [0],
      serverMs: null,
      text: "first line\nsecond line",
      content: null,
      model: "gpt-5",
      inputTokens: 10,
      outputTokens: 20,
      finishReason: "stop",
    };
    const { rerender } = render(
      <App>
        <CompactRow row={row} expanded={false} onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-think")).toHaveTextContent(
      "Thinking · first line",
    );
    expect(screen.getByTestId("console-row-think")).not.toHaveTextContent("second line");

    rerender(
      <App>
        <CompactRow row={row} expanded={false} onToggle={vi.fn()} liveText={"a\nb\nc"} />
      </App>,
    );
    expect(screen.getByTestId("console-row-think")).toHaveTextContent("Thinking… · c");
  });

  it("plan row: update_plan with reason vs planner; expanded lists steps with status glyphs", () => {
    const updateRow: PlanRow = {
      id: "plan:0:0",
      kind: "plan",
      seq: 0,
      step: 1,
      status: "ok",
      durationMs: 100,
      eventIndexes: [0],
      serverMs: null,
      source: "update_plan",
      callId: "c1",
      plannerSeq: 1,
      stepsTotal: 3,
      goal: "ship feature",
      reason: "客户档案已查完。下一步联系客户",
      plan: {
        goal: "ship feature",
        steps: [
          { id: "1", description: "查档案", status: "completed" },
          { id: "2", description: "写方案", status: "in_progress" },
          { id: "3", description: "复核", status: "pending" },
        ],
      },
    };
    const { unmount } = render(
      <App>
        <CompactRow row={updateRow} expanded onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-plan")).toHaveTextContent(
      "Plan · updated to 3 steps · 客户档案已查完",
    );
    expect(screen.getByTestId("console-row-plan")).not.toHaveTextContent("下一步联系客户");
    const detail = screen.getByTestId("console-row-detail");
    expect(detail).toHaveTextContent("✓");
    expect(detail).toHaveTextContent("◐");
    expect(detail).toHaveTextContent("○");
    unmount();

    const createRow: PlanRow = {
      id: "plan:1",
      kind: "plan",
      seq: 1,
      step: null,
      status: "ok",
      durationMs: null,
      eventIndexes: [],
      serverMs: null,
      source: "planner",
      callId: null,
      plannerSeq: null,
      stepsTotal: 3,
      goal: "ship feature",
      reason: null,
      plan: null,
    };
    render(
      <App>
        <CompactRow row={createRow} expanded={false} onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-plan")).toHaveTextContent("Plan drafted · 3 steps");
  });

  it("marker rows (error/compaction) are not expandable and carry the status colour", () => {
    const row: MarkerRow = {
      id: "error:5",
      kind: "error",
      seq: 5,
      step: null,
      status: "error",
      durationMs: null,
      eventIndexes: [2],
      serverMs: null,
      text: "运行错误",
    };
    expect(rowIsExpandable(row)).toBe(false);
    const onToggle = vi.fn();
    render(
      <App>
        <CompactRow row={row} expanded={false} onToggle={onToggle} />
      </App>,
    );
    const el = screen.getByTestId("console-row-error");
    expect(el).toHaveAttribute("data-status", "error");
    expect(el).not.toHaveAttribute("aria-expanded");
    expect(el.getAttribute("role")).not.toBe("button");
    expect(screen.queryByTestId("console-row-detail")).not.toBeInTheDocument();
  });

  it("inspect button renders only when onInspect is given and calls it", async () => {
    const row: ThinkRow = {
      id: "think:0",
      kind: "think",
      seq: 0,
      step: 1,
      status: "ok",
      durationMs: 100,
      eventIndexes: [],
      serverMs: null,
      text: "hello",
      content: null,
      model: null,
      inputTokens: 0,
      outputTokens: 0,
      finishReason: null,
    };
    const onToggle = vi.fn();
    const { rerender } = render(
      <App>
        <CompactRow row={row} expanded={false} onToggle={onToggle} />
      </App>,
    );
    expect(screen.queryByTestId("console-row-inspect")).not.toBeInTheDocument();

    const onInspect = vi.fn();
    rerender(
      <App>
        <CompactRow row={row} expanded={false} onToggle={onToggle} onInspect={onInspect} />
      </App>,
    );
    await userEvent.click(screen.getByTestId("console-row-inspect"));
    expect(onInspect).toHaveBeenCalledTimes(1);
    // The inspect click must not also toggle the row (nested-control click,
    // stopPropagation required).
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("memory / reflect / subagent rows render their one-line copy", () => {
    const memoryRow: MemoryRow = {
      id: "memory:0",
      kind: "memory",
      seq: 0,
      step: null,
      status: "ok",
      durationMs: null,
      eventIndexes: [],
      serverMs: null,
      direction: "recall",
      count: 4,
      detail: { memories: [] },
    };
    const { unmount: unmountMemory } = render(
      <App>
        <CompactRow row={memoryRow} expanded={false} onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-memory")).toHaveTextContent("Memory recall · 4");
    unmountMemory();

    const reflectRow: ReflectRow = {
      id: "reflect:0",
      kind: "reflect",
      seq: 0,
      step: null,
      status: "warn",
      durationMs: null,
      eventIndexes: [],
      serverMs: null,
      verdict: "revise",
      detail: {},
    };
    const { unmount: unmountReflect } = render(
      <App>
        <CompactRow row={reflectRow} expanded={false} onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-reflect")).toHaveTextContent("Reflection · revise");
    unmountReflect();

    const subagentRow: SubagentRow = {
      id: "subagent:0:0:0",
      kind: "subagent",
      seq: 0,
      step: 1,
      status: "running",
      durationMs: null,
      eventIndexes: [],
      serverMs: null,
      worker: {
        workerId: "w1",
        parentWorkerId: null,
        parentToolCallId: "c1",
        label: "researcher",
        agentRef: "agent:research",
        depth: 1,
        role: null,
        taskExcerpt: "look into X",
        maxSteps: null,
        status: "running",
        steps: [],
        children: [],
        summary: null,
      },
      parentEntryId: "c1",
    };
    render(
      <App>
        <CompactRow row={subagentRow} expanded={false} onToggle={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-row-subagent")).toHaveTextContent(
      "Sub-agent · researcher",
    );
  });
});
