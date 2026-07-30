/**
 * GanttTimeline tests — RTL, pinned to the confirmed interactive prototype's
 * behaviour (see task-2-brief.md Step 1): embedded/expanded density switch,
 * start/duration → percentage bar geometry (+ concurrent overlap), one-row-
 * at-a-time detail expansion, final-kind semantic color + marker ticks, and
 * the in-progress (`durationMs === null`) growing-vs-interrupted state.
 *
 * Style assertions stay non-brittle: class-name / attribute checks, never
 * pixel math beyond the documented left/width percentage contract.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../../../i18n";

import { GanttTimeline } from "../GanttTimeline";
import type { GanttMarker, GanttModel, GanttRow } from "../../../api/gantt_timeline";
import type { TimelineItem } from "../../../api/timeline";

// GanttTimeline never reads inside `detail` — it only forwards the row to
// `renderDetail` — so one fixed minimal AgentStep stands in for every row.
const FAKE_ITEM: TimelineItem = {
  kind: "agent",
  seq: 0,
  receivedAt: "",
  stepCount: 1,
  node: "agent",
  model: null,
  finishReason: null,
  reasoning: null,
  content: null,
  inputTokens: 0,
  outputTokens: 0,
  totalTokens: 0,
  tools: [],
  hasError: false,
  durationMs: null,
};

function row(overrides: Partial<GanttRow> & Pick<GanttRow, "key" | "label">): GanttRow {
  return {
    kind: "agent",
    depth: 0,
    startMs: 0,
    durationMs: 1000,
    hasError: false,
    detail: { type: "item", item: FAKE_ITEM },
    ...overrides,
  };
}

function buildModel(rows: GanttRow[], markers: GanttMarker[] = [], totalMs = 10_000): GanttModel {
  return { rows, markers, totalMs, degraded: false };
}

describe("GanttTimeline", () => {
  it("嵌入态:标签列 Tooltip 包裹、时长标签带 hover-only class;放大态:时长常显", async () => {
    const model = buildModel([
      row({ key: "r1", label: "步骤 1 · 思考", model: "glm-5.2", startMs: 0, durationMs: 5000 }),
    ]);

    const embedded = render(
      <GanttTimeline model={model} variant="embedded" renderDetail={() => null} />,
    );
    // Embedded density collapses the model name out of the visible label —
    // it only surfaces via the tooltip.
    expect(screen.queryByText("glm-5.2")).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByTestId("gantt-label-r1"));
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("glm-5.2"));
    expect(screen.getByTestId("gantt-dur-r1").className).toContain("gantt-dur-hover");
    embedded.unmount();

    const expanded = render(
      <GanttTimeline model={model} variant="expanded" renderDetail={() => null} />,
    );
    expect(screen.getByTestId("gantt-dur-r1").className).not.toContain("gantt-dur-hover");
    // Task 2 self-review follow-up — the expanded density surfaces the model
    // name directly in the row (not just via the tooltip the embedded
    // density falls back to).
    expect(screen.getByText("glm-5.2")).toBeInTheDocument();
    expanded.unmount();
  });

  it("条形 left/width 按 startMs/durationMs 百分比;并发两行条形区间重叠", () => {
    const model = buildModel(
      [
        row({ key: "agent-1", label: "步骤 1", startMs: 0, durationMs: 5000 }),
        row({ key: "tool-a", label: "web_search A", kind: "tool", depth: 1, startMs: 5000, durationMs: 3000 }),
        row({ key: "tool-b", label: "web_search B", kind: "tool", depth: 1, startMs: 5000, durationMs: 2000 }),
      ],
      [],
      10_000,
    );
    render(<GanttTimeline model={model} variant="expanded" renderDetail={() => null} />);

    const agentBar = screen.getByTestId("gantt-bar-agent-1");
    expect(agentBar.style.left).toBe("0%");
    expect(agentBar.style.width).toBe("50%");

    const toolABar = screen.getByTestId("gantt-bar-tool-a");
    expect(toolABar.style.left).toBe("50%");
    expect(toolABar.style.width).toBe("30%");

    const toolBBar = screen.getByTestId("gantt-bar-tool-b");
    expect(toolBBar.style.left).toBe("50%");
    expect(toolBBar.style.width).toBe("20%");

    // Concurrent rows (same startMs) share the same left offset — the
    // overlap a vertical step-list can't show.
    expect(toolABar.style.left).toBe(toolBBar.style.left);
  });

  it("点击行渲染 renderDetail 内容,再点收起;一次仅一行展开", () => {
    const model = buildModel([
      row({ key: "r1", label: "行一" }),
      row({ key: "r2", label: "行二", startMs: 1000 }),
    ]);
    render(
      <GanttTimeline
        model={model}
        variant="expanded"
        renderDetail={(r) => <div data-testid="detail-body">{r.key}</div>}
      />,
    );

    expect(screen.queryByTestId("detail-body")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("gantt-row-r1"));
    expect(screen.getByTestId("detail-body")).toHaveTextContent("r1");

    // Opening the second row closes the first — only one open at a time.
    fireEvent.click(screen.getByTestId("gantt-row-r2"));
    expect(screen.getAllByTestId("detail-body")).toHaveLength(1);
    expect(screen.getByTestId("detail-body")).toHaveTextContent("r2");

    // Clicking the open row again collapses it.
    fireEvent.click(screen.getByTestId("gantt-row-r2"));
    expect(screen.queryByTestId("detail-body")).not.toBeInTheDocument();
  });

  it("kind=final 行携带 final 语义色 class;marker 渲染为轴上刻度并带 Tooltip", async () => {
    const model = buildModel(
      [row({ key: "final-1", label: "最终输出", kind: "final", startMs: 8000, durationMs: 2000 })],
      [{ atMs: 4300, kind: "error", text: "web_search 失败:超时" }],
      10_000,
    );
    render(<GanttTimeline model={model} variant="expanded" renderDetail={() => null} />);

    expect(screen.getByTestId("gantt-bar-final-1").className).toContain("ew-gantt-bar--final");

    // I2 — a native `title` attribute has no hover delay/dismiss and no
    // keyboard/focus support; replaced by an antd Tooltip on the ±4px hit
    // area (same mouseEnter/waitFor pattern as the label-column tooltip
    // test above).
    const marker = screen.getByTestId("gantt-marker");
    fireEvent.mouseEnter(marker);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("web_search 失败:超时"));
  });

  it("I3② 巨大 totalMs(退化锚定异常场景)不生成海量刻度,上限 200", () => {
    const model = buildModel(
      [row({ key: "r1", label: "步骤 1", startMs: 0, durationMs: 1000 })],
      [],
      10_000_000_000, // pathological — uncapped would be ~333k 30s-step ticks
    );
    const { container } = render(
      <GanttTimeline model={model} variant="expanded" renderDetail={() => null} />,
    );
    expect(container.querySelectorAll(".ew-gantt-tick").length).toBeLessThanOrEqual(200);
  });

  it("durationMs=null 行渲染生长条 class(running)或中断态(!running)", () => {
    const model = buildModel([
      row({ key: "live-1", label: "步骤 2", startMs: 3000, durationMs: null }),
    ]);

    const runningRender = render(
      <GanttTimeline model={model} variant="expanded" running renderDetail={() => null} />,
    );
    expect(screen.getByTestId("gantt-bar-live-1").className).toContain("ew-gantt-bar--running");
    runningRender.unmount();

    const interruptedRender = render(
      <GanttTimeline model={model} variant="expanded" running={false} renderDetail={() => null} />,
    );
    expect(screen.getByTestId("gantt-bar-live-1").className).toContain("ew-gantt-bar--interrupted");
    interruptedRender.unmount();
  });
});
