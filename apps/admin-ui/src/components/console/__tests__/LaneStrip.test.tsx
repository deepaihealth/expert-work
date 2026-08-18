/**
 * 泳道 v2 组件测试(PR-A.1 Task 5)—— 细泳道 / 提示气泡 / hover 联动 / 拖选 /
 * 刻度。PR-A 的三条 `it` 行为已被 spec §八.7 改写(块不再来自 gantt 行,而是
 * 每条轨迹行一个块),断言随之重写,逐条对照见 task-5-report.md。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import { trajectoryRowsOf, type TrajectoryRow } from "../../../api/trajectory_rows";
import { laneProjection } from "../lane_strip_model";
import { LaneStrip, type LaneStripProps } from "../LaneStrip";

// jsdom 25 ships no `PointerEvent`, so testing-library falls back to the base
// `Event` constructor — which silently drops `clientX`, making every drag a
// zero-pixel one. A `MouseEvent`-derived stand-in (MouseEvent *does* honour
// clientX) restores the coordinates the drag maths reads. `setPointerCapture`
// is likewise absent from jsdom, hence the component's optional call.
class PointerEventPolyfill extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}
if (!("PointerEvent" in window)) {
  (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventPolyfill;
}

const BASE_MS = 1_700_000_000_000;
let seqCounter = 0;

function ev(event: string, data: unknown, ms: number | null): SseEvent {
  seqCounter += 1;
  return {
    id: ms === null ? null : `${BASE_MS + ms}-${seqCounter}`,
    event,
    data,
    rawData: "",
    receivedAt: "2026-01-01T00:00:00.000Z",
  };
}
function upd(node: string, channels: Record<string, unknown>, ms: number | null): SseEvent {
  return ev("updates", { [node]: channels }, ms);
}
function call(id: string, name: string) {
  return { id, name, args: {} };
}
function result(id: string, name: string, durationMs: number, status: "success" | "error" = "success") {
  return {
    type: "tool",
    tool_call_id: id,
    name,
    content: status === "error" ? "炸了" : "ok",
    status,
    additional_kwargs: { duration_ms: durationMs },
  };
}

const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

/** 9 行:user / think / tool / tool / think / tool / tool(error) / think / assistant。 */
function nineRowEvents(): SseEvent[] {
  return [
    upd("agent", {
      step_count: 1,
      _duration_ms: 400,
      messages: [{
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "先查客户档案" },
        tool_calls: [call("c1", "query_crm"), call("c2", "search")],
      }],
    }, 1000),
    upd("tools", { messages: [result("c1", "query_crm", 300)] }, 1400),
    upd("tools", { messages: [result("c2", "search", 200)] }, 1700),
    upd("agent", {
      step_count: 2,
      _duration_ms: 500,
      messages: [{
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "再算一次" },
        tool_calls: [call("c3", "calc"), call("c4", "boom")],
      }],
    }, 2300),
    upd("tools", { messages: [result("c3", "calc", 100)] }, 2400),
    upd("tools", { messages: [result("c4", "boom", 150, "error")] }, 2600),
    upd("agent", { step_count: 3, _duration_ms: 600, messages: [{ type: "ai", content: "结论" }] }, 3200),
  ];
}

/** 轨道量尺:左 0 / 宽 300px —— 域坐标 = clientX / 300 × total。 */
const RECT_300 = {
  x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 14, width: 300, height: 14,
  toJSON: () => ({}),
} as DOMRect;

const summaryOf = (row: TrajectoryRow): string => `摘要-${row.id}`;

function props(overrides: Partial<LaneStripProps> = {}): LaneStripProps {
  const events = overrides.events ?? nineRowEvents();
  const rows = overrides.rows ?? trajectoryRowsOf(events, INPUT, "结论", "done");
  return {
    rows,
    events,
    running: false,
    mode: "sequence",
    selectedRowId: null,
    hoveredRowId: null,
    onHoverRow: vi.fn(),
    onSelectRow: vi.fn(),
    range: null,
    onRangeChange: vi.fn(),
    summaryOf,
    ...overrides,
  };
}

function blockOf(rowId: string): HTMLElement {
  const el = screen
    .getAllByTestId("console-lane-block")
    .find((b) => b.getAttribute("data-row-id") === rowId);
  if (!el) throw new Error(`no lane block for row ${rowId}`);
  return el;
}

describe("LaneStrip", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    vi.restoreAllMocks();
    // jsdom 原本就没有这两个方法,`restoreAllMocks` 管不到 defineProperty
    // 装上去的桩,手动摘掉免得漏给下一条用例。
    Reflect.deleteProperty(HTMLElement.prototype, "setPointerCapture");
    Reflect.deleteProperty(HTMLElement.prototype, "releasePointerCapture");
    await i18n.changeLanguage(priorLang);
  });

  it("renders one block per row and clicking a block selects its row", async () => {
    const p = props();
    render(<LaneStrip {...p} />);

    const blocks = screen.getAllByTestId("console-lane-block");
    expect(blocks).toHaveLength(9);
    // 顺序模式下每块等宽 = 1/9 域,error 块带 data-error。
    const think = blockOf("think:0");
    expect(think.style.left).toBe(`${(1 / 9) * 100}%`);
    expect(think.style.width).toBe(`${(1 / 9) * 100}%`);
    expect(blockOf("tool:1:1").getAttribute("data-error")).toBe("true");
    expect(think.getAttribute("data-error")).toBeNull();

    await userEvent.click(think);
    expect(p.onSelectRow).toHaveBeenCalledWith("think:0");
  });

  it("duration mode with unresolvable gantt timing still draws every row (degraded to sequence layout)", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "想" }, tool_calls: [] }],
      }, null),
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "答" }] }, null),
    ];
    const rows = trajectoryRowsOf(events, INPUT, "答", "done");
    render(<LaneStrip {...props({ events, rows, mode: "duration" })} />);

    const blocks = screen.getAllByTestId("console-lane-block");
    expect(blocks).toHaveLength(rows.length);
    for (const b of blocks) expect(b.tagName).toBe("BUTTON");
    expect(screen.getByTestId("console-lane-strip").getAttribute("data-degraded")).toBe("true");
  });

  it("renders nothing when there are no rows", () => {
    const { container } = render(<LaneStrip {...props({ rows: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders three labelled lanes and one block per row with data-index", () => {
    render(<LaneStrip {...props()} />);

    expect(screen.getByText("用户")).toBeInTheDocument();
    expect(screen.getByText("模型")).toBeInTheDocument();
    expect(screen.getByText("工具")).toBeInTheDocument();

    const blocks = screen.getAllByTestId("console-lane-block");
    expect(blocks.map((b) => Number(b.getAttribute("data-index"))).sort((a, b) => a - b))
      .toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(blockOf("user").getAttribute("data-lane")).toBe("user");
    expect(blockOf("think:0").getAttribute("data-lane")).toBe("model");
    expect(blockOf("tool:0:0").getAttribute("data-lane")).toBe("tools");
    expect(blockOf("tool:0:0").getAttribute("data-kind")).toBe("tool");
  });

  it("hover on a block calls onHoverRow(id) then null on leave, and the block reflects hoveredRowId", () => {
    const p = props();
    const { rerender } = render(<LaneStrip {...p} />);

    const block = blockOf("tool:0:1");
    expect(block.getAttribute("data-hovered")).toBeNull();

    fireEvent.mouseOver(block);
    expect(p.onHoverRow).toHaveBeenCalledWith("tool:0:1");
    fireEvent.mouseOut(block);
    expect(p.onHoverRow).toHaveBeenLastCalledWith(null);

    rerender(<LaneStrip {...p} hoveredRowId="tool:0:1" />);
    expect(blockOf("tool:0:1").getAttribute("data-hovered")).toBe("true");
    expect(blockOf("tool:0:0").getAttribute("data-hovered")).toBeNull();
  });

  it("selected block gets data-selected", () => {
    const { rerender } = render(<LaneStrip {...props()} />);
    expect(blockOf("think:1").getAttribute("data-selected")).toBeNull();

    rerender(<LaneStrip {...props({ selectedRowId: "think:1" })} />);
    expect(blockOf("think:1").getAttribute("data-selected")).toBe("true");
    expect(blockOf("think:0").getAttribute("data-selected")).toBeNull();
  });

  it("blocks outside `range` are dimmed", () => {
    render(<LaneStrip {...props({ range: { from: 3, to: 5 } })} />);

    const dimmed = screen
      .getAllByTestId("console-lane-block")
      .filter((b) => b.getAttribute("data-dimmed") === "true")
      .map((b) => Number(b.getAttribute("data-index")))
      .sort((a, b) => a - b);
    expect(dimmed).toEqual([1, 2, 6, 7, 8, 9]);
    expect(screen.getByTestId("console-lane-range")).toBeInTheDocument();
  });

  it("drag on the track calls onRangeChange with the row span; a <4px move does not", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_300);

    const p = props();
    render(<LaneStrip {...p} />);
    const track = screen.getByTestId("console-lane-track");

    // 域宽 9 行 / 轨道 300px → 100px = 域 3、200px = 域 6 → 与 [3,6) 相交的
    // 是第 4/5/6 行。
    fireEvent.pointerDown(track, { clientX: 100, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 200, pointerId: 1 });
    fireEvent.pointerUp(track, { clientX: 200, pointerId: 1 });
    expect(p.onRangeChange).toHaveBeenCalledWith({ from: 4, to: 6 });

    (p.onRangeChange as ReturnType<typeof vi.fn>).mockClear();
    fireEvent.pointerDown(track, { clientX: 100, pointerId: 1 });
    fireEvent.pointerUp(track, { clientX: 102, pointerId: 1 });
    expect(p.onRangeChange).not.toHaveBeenCalled();
  });

  it("the running turn's tail block is marked data-live", () => {
    const events = nineRowEvents().slice(0, 1);
    const rows = trajectoryRowsOf(events, INPUT, null, "running");
    render(<LaneStrip {...props({ events, rows, running: true })} />);

    const live = screen
      .getAllByTestId("console-lane-block")
      .filter((b) => b.getAttribute("data-live") === "true")
      .map((b) => b.getAttribute("data-row-id"));
    // 末行 = 还没回结果的工具调用。
    expect(live).toEqual([rows[rows.length - 1].id]);
  });

  it("a sub-threshold press never captures the pointer, so the block's own click still selects", () => {
    // 复评 Critical:pointerdown 就 setPointerCapture 会把随后的 pointerup /
    // click 重定向到捕获容器,真实浏览器里块按钮的 onClick 永远不触发。
    const captured = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true, writable: true, value: captured,
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_300);

    const p = props();
    render(<LaneStrip {...p} />);
    const block = blockOf("think:0");

    fireEvent.pointerDown(block, { clientX: 100, pointerId: 1 });
    fireEvent.pointerUp(block, { clientX: 102, pointerId: 1 });
    fireEvent.click(block);

    expect(captured).not.toHaveBeenCalled();
    expect(p.onSelectRow).toHaveBeenCalledWith("think:0");
    expect(p.onRangeChange).not.toHaveBeenCalled();
  });

  it("a real drag captures the pointer once (on crossing the threshold) and ends with a range", () => {
    const captured = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true, writable: true, value: captured,
    });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      configurable: true, writable: true, value: vi.fn(),
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_300);

    const p = props();
    render(<LaneStrip {...p} />);
    const track = screen.getByTestId("console-lane-track");

    fireEvent.pointerDown(track, { clientX: 100, pointerId: 1 });
    expect(captured).not.toHaveBeenCalled();
    fireEvent.pointerMove(track, { clientX: 150, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 200, pointerId: 1 });
    expect(captured).toHaveBeenCalledTimes(1);
    expect(captured).toHaveBeenCalledWith(1);
    fireEvent.pointerUp(track, { clientX: 200, pointerId: 1 });

    expect(p.onRangeChange).toHaveBeenCalledWith({ from: 4, to: 6 });
  });

  it("pointercancel mid-drag aborts: no range, no draft mask left behind", () => {
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true, writable: true, value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      configurable: true, writable: true, value: vi.fn(),
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_300);

    const p = props();
    render(<LaneStrip {...p} />);
    const track = screen.getByTestId("console-lane-track");

    fireEvent.pointerDown(track, { clientX: 100, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 200, pointerId: 1 });
    expect(screen.getByTestId("console-lane-draft")).toBeInTheDocument();

    fireEvent.pointerCancel(track, { clientX: 200, pointerId: 1 });
    expect(screen.queryByTestId("console-lane-draft")).not.toBeInTheDocument();
    expect(p.onRangeChange).not.toHaveBeenCalled();

    // 拖选状态已清空 —— 之后单独一次抬起不该补出区间。
    fireEvent.pointerUp(track, { clientX: 260, pointerId: 1 });
    expect(p.onRangeChange).not.toHaveBeenCalled();
  });

  it("double-click clears the range", () => {
    const p = props({ range: { from: 3, to: 5 } });
    render(<LaneStrip {...p} />);

    fireEvent.doubleClick(screen.getByTestId("console-lane-track"));
    expect(p.onRangeChange).toHaveBeenCalledWith(null);
  });

  it("shows the tooltip with kind · #index · summary", async () => {
    render(<LaneStrip {...props()} />);

    const block = blockOf("tool:0:0");
    expect(block.getAttribute("aria-label")).toBe("TOOL · #3 · 摘要-tool:0:0");

    fireEvent.mouseOver(block);
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("TOOL · #3 · 摘要-tool:0:0");
    expect(tip).toHaveTextContent("点击选中 · 拖选过滤 · 双击复位");
  });

  it("mode=duration renders time ticks (0s … total) and mode=sequence renders #k ticks", () => {
    const events = nineRowEvents();
    const rows = trajectoryRowsOf(events, INPUT, "结论", "done");
    const { rerender } = render(<LaneStrip {...props({ events, rows, mode: "sequence" })} />);

    expect(screen.getAllByTestId("console-lane-tick").map((n) => n.textContent))
      .toEqual(["#1", "#3", "#5", "#7", "#9"]);

    rerender(<LaneStrip {...props({ events, rows, mode: "duration" })} />);
    const p = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });
    expect(p.degraded).toBe(false);
    const labels = screen.getAllByTestId("console-lane-tick").map((n) => n.textContent);
    expect(labels).toHaveLength(5);
    expect(labels[0]).toBe("0ms");
    expect(labels[4]).toBe("2.6s");
  });
});
