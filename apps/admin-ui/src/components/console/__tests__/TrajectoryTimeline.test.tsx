/**
 * 概览时间轴组件测试(PR-A.2 Task 7)—— 三泳道投影、选区(拖 / 点块 / 点空白 /
 * 清除)、缩放平移、搜索淡出、悬停联动、「加载更早」与空态、提示气泡。
 * 几何靠 mock 掉的 `getBoundingClientRect`(轨道 = 左 0 宽 1000px)。
 */
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18n from "../../../i18n";

import type { TrajectoryRow } from "../../../api/trajectory_rows";
import { deriveTimeline, formatClock, type TimelineMode } from "../ledger_timeline";
import type { LedgerLane, LedgerRecord } from "../ledger_types";
import { TrajectoryTimeline, type TrajectoryTimelineProps } from "../TrajectoryTimeline";

// jsdom 25 没有 `PointerEvent`,testing-library 会退回基类 `Event` —— 它把
// `clientX` 静默丢掉,于是每次拖拽都成了零像素。用 `MouseEvent` 派生的替身
// 补上坐标(与 LaneStrip.test.tsx 同一套)。
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

const RECT_1000 = { left: 0, top: 0, right: 1000, bottom: 50, width: 1000, height: 50, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;

function mockTrack(): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_1000);
}

function rec(
  index: number,
  lane: LedgerLane,
  kind: LedgerRecord["kind"],
  startedAt: number | null,
  endedAt: number | null,
  extra: Partial<LedgerRecord> = {},
): LedgerRecord {
  const row = {
    id: `${kind}:${index}`, seq: index, step: null, status: "ok",
    durationMs: null, eventIndexes: [], serverMs: null, kind, text: "",
  } as unknown as TrajectoryRow;
  return {
    id: `t/${kind}:${index}`, index, turnKey: "t", turnSeq: 0, runId: null,
    turnStart: index === 0, turnEnd: false, requestNo: null, ownerRequestNo: null,
    parentId: null, kind, lane, isError: false, running: false, startedAt, endedAt,
    text: "", resultText: null, row, events: [], placeholder: null, ...extra,
  };
}

/** 6 条 · 2 轮:顺序域 [0,6),轨道 1000px → 每块 166.67px。 */
function sixRecords(): LedgerRecord[] {
  return [
    rec(0, 0, "user", 1000, 1000),
    rec(1, 1, "assistant", 1000, 1400),
    rec(2, 2, "tool", 1400, 1600, { isError: true }),
    rec(3, 1, "assistant", 3000, 3200, { turnSeq: 1, turnStart: true }),
    rec(4, 2, "tool", 3200, 3500, { turnSeq: 1 }),
    rec(5, 1, "assistant", 3500, null, { turnSeq: 1, running: true }),
  ];
}

function props(
  over: Partial<TrajectoryTimelineProps> = {},
  mode: TimelineMode = "sequence",
): TrajectoryTimelineProps {
  const records = over.records ?? sixRecords();
  return {
    model: deriveTimeline(records, mode),
    records,
    range: null,
    onRangeChange: vi.fn(),
    selectedIndex: null,
    hoveredIndex: null,
    onHoverIndex: vi.fn(),
    onSelectRecord: vi.fn(),
    onFocusRecord: vi.fn(),
    searchMatches: null,
    hasEarlier: false,
    loadingEarlier: false,
    onLoadEarlier: vi.fn(),
    ...over,
  };
}

function blocks(): HTMLElement[] {
  return screen.getAllByTestId("console-lane-block");
}
function blockAt(index: number): HTMLElement {
  const found = blocks().find((b) => b.getAttribute("data-index") === String(index));
  if (found === undefined) throw new Error(`no block for index ${index}`);
  return found;
}
function track(): HTMLElement {
  return screen.getByTestId("console-lane-track");
}
function pct(el: HTMLElement, prop: string): number {
  return Number.parseFloat(el.style.getPropertyValue(prop));
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TrajectoryTimeline · 投影", () => {
  it("三条泳道标签 + 每条记录一个块,带 data-lane / data-kind / data-index", () => {
    render(<TrajectoryTimeline {...props()} />);

    expect(screen.getByText("输入")).toBeInTheDocument();
    expect(screen.getByText("模型")).toBeInTheDocument();
    expect(screen.getByText("工具")).toBeInTheDocument();

    const strip = screen.getByTestId("console-lane-strip");
    expect(strip).toHaveAttribute("data-mode", "sequence");
    expect(strip).toHaveAttribute("aria-label", "轨迹时间轴");
    expect(track()).toHaveAttribute("tabindex", "0");
    expect(track()).toHaveAttribute("aria-label", "时间轴总览;横向拖动聚焦记录");
    const all = blocks();
    expect(all).toHaveLength(6);
    expect(all.map((b) => b.getAttribute("data-index"))).toEqual(["0", "1", "2", "3", "4", "5"]);
    expect(all.map((b) => b.getAttribute("data-lane"))).toEqual(["0", "1", "2", "1", "2", "1"]);
    expect(all.map((b) => b.getAttribute("data-kind")))
      .toEqual(["user", "assistant", "tool", "assistant", "tool", "assistant"]);
    // 顺序域:第 i 块占 [i/6, (i+1)/6)。
    expect(pct(all[1], "--traj-span-left")).toBeCloseTo(100 / 6, 3);
    expect(pct(all[1], "--traj-span-width")).toBeCloseTo(100 / 6, 3);
    // 第二轮首条记录处一条轮边界(首轮的落在域左端,过滤掉)。
    const boundaries = screen.getAllByTestId("console-lane-turn-boundary");
    expect(boundaries).toHaveLength(1);
    expect(boundaries[0]).toHaveAttribute("data-turn", "1");
  });

  it("失败记录的块标 data-error", () => {
    render(<TrajectoryTimeline {...props()} />);

    expect(blockAt(2)).toHaveAttribute("data-error", "true");
    expect(blockAt(1).hasAttribute("data-error")).toBe(false);
  });

  it("运行中的记录标 data-live", () => {
    render(<TrajectoryTimeline {...props()} />);

    expect(blocks().filter((b) => b.getAttribute("data-live") === "true").map((b) => b.getAttribute("data-index")))
      .toEqual(["5"]);
  });

  it("selectedIndex 的块标 data-current", () => {
    render(<TrajectoryTimeline {...props({ selectedIndex: 3 })} />);

    expect(blocks().filter((b) => b.getAttribute("data-current") === "true").map((b) => b.getAttribute("data-index")))
      .toEqual(["3"]);
  });

  it("searchMatches 给每块打 data-search-match;无查询(null)时整个属性不出现", () => {
    const { rerender } = render(<TrajectoryTimeline {...props({ searchMatches: new Set([1, 4]) })} />);

    expect(blocks().map((b) => b.getAttribute("data-search-match")))
      .toEqual(["false", "true", "false", "false", "true", "false"]);

    rerender(<TrajectoryTimeline {...props({ searchMatches: null })} />);
    expect(blocks().every((b) => !b.hasAttribute("data-search-match"))).toBe(true);
  });

  it("要时长但有记录缺时序 → 退化成顺序排布并标 data-degraded", () => {
    const records = sixRecords().map((r) => (r.index === 4 ? { ...r, startedAt: null } : r));
    render(<TrajectoryTimeline {...props({ records, model: deriveTimeline(records, "duration") })} />);

    const strip = screen.getByTestId("console-lane-strip");
    expect(strip).toHaveAttribute("data-mode", "duration");
    expect(strip).toHaveAttribute("data-degraded", "true");
    // 退化后是等宽顺序块 —— 每块占域的 1/6。
    expect(pct(blockAt(1), "--traj-span-width")).toBeCloseTo(100 / 6, 3);
  });

  it("model 为 null 时出空态,不出块", () => {
    render(<TrajectoryTimeline {...props({ model: null, records: [] })} />);

    expect(screen.getByTestId("console-lane-empty")).toHaveTextContent("没有时序数据");
    expect(screen.queryAllByTestId("console-lane-block")).toHaveLength(0);
  });
});

describe("TrajectoryTimeline · 选区", () => {
  it("range 内的块 data-selected=true、外面 false,选区与两侧压暗块的几何按域比例", () => {
    // 顺序域 [0,6):选区 [2.2, 2.8] 只与第 2 条(span [2,3))相交。
    render(<TrajectoryTimeline {...props({ range: { start: 2.2, end: 2.8 } })} />);

    expect(blocks().map((b) => b.getAttribute("data-selected")))
      .toEqual(["false", "false", "true", "false", "false", "false"]);

    const range = screen.getByTestId("console-lane-range");
    expect(Number.parseFloat(range.style.left)).toBeCloseTo(2.2 / 6 * 100, 3);
    expect(Number.parseFloat(range.style.width)).toBeCloseTo(0.6 / 6 * 100, 3);
    expect(screen.getByTestId("console-lane-range-edges")).toBeInTheDocument();

    const dims = screen.getAllByTestId("console-lane-dim");
    expect(dims).toHaveLength(2);
    expect(Number.parseFloat(dims[0].style.width)).toBeCloseTo(2.2 / 6 * 100, 3);
    expect(Number.parseFloat(dims[1].style.left)).toBeCloseTo(2.8 / 6 * 100, 3);
  });

  it("拖动 ≥ 3px 提交有序区间(反向拖也归一)", () => {
    mockTrack();
    const p = props();
    const { rerender } = render(<TrajectoryTimeline {...p} />);

    // 1000px 轨道 / 域 6:x=100 → 0.6,x=400 → 2.4。
    fireEvent.pointerDown(track(), { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(track(), { clientX: 400, pointerId: 1 });
    fireEvent.pointerUp(track(), { clientX: 400, pointerId: 1, button: 0 });
    expect(p.onRangeChange).toHaveBeenCalledTimes(1);
    const first = (p.onRangeChange as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(first.start).toBeCloseTo(0.6, 6);
    expect(first.end).toBeCloseTo(2.4, 6);

    const q = props();
    rerender(<TrajectoryTimeline {...q} />);
    fireEvent.pointerDown(track(), { clientX: 400, pointerId: 2, button: 0 });
    fireEvent.pointerMove(track(), { clientX: 100, pointerId: 2 });
    fireEvent.pointerUp(track(), { clientX: 100, pointerId: 2, button: 0 });
    const back = (q.onRangeChange as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(back.start).toBeCloseTo(0.6, 6);
    expect(back.end).toBeCloseTo(2.4, 6);
  });

  it("指针捕获只在位移过 3px 阈值之后才拿", () => {
    // PR-A.1 Task 5 的坑:按下就捕获会把随后的 pointerup 重定向到轨道,
    // 真实浏览器里「点块 = 选中记录」直接失效;jsdom 照不出来,靠 spy 钉住。
    const captured = vi.fn();
    const released = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, writable: true, value: captured });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", { configurable: true, writable: true, value: released });
    mockTrack();

    render(<TrajectoryTimeline {...props()} />);
    fireEvent.pointerDown(track(), { clientX: 100, pointerId: 1, button: 0 });
    expect(captured).not.toHaveBeenCalled();
    fireEvent.pointerMove(track(), { clientX: 102, pointerId: 1 });
    expect(captured).not.toHaveBeenCalled();
    fireEvent.pointerMove(track(), { clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(track(), { clientX: 300, pointerId: 1 });
    expect(captured).toHaveBeenCalledTimes(1);
    expect(captured).toHaveBeenCalledWith(1);
    fireEvent.pointerUp(track(), { clientX: 300, pointerId: 1, button: 0 });
    expect(released).toHaveBeenCalledWith(1);
  });

  it("点块(位移 < 3px)= 清选区 + 选中该记录,不产生区间", () => {
    mockTrack();
    const p = props({ range: { start: 1, end: 2 } });
    render(<TrajectoryTimeline {...p} />);

    const block = blockAt(4);
    fireEvent.pointerDown(block, { clientX: 700, pointerId: 1, button: 0 });
    fireEvent.pointerUp(block, { clientX: 701, pointerId: 1, button: 0 });

    expect(p.onRangeChange).toHaveBeenCalledTimes(1);
    expect(p.onRangeChange).toHaveBeenCalledWith(null);
    expect(p.onSelectRecord).toHaveBeenCalledWith(4);
    expect(p.onFocusRecord).not.toHaveBeenCalled();
  });

  it("点空白 = 开一条记录宽的最小选区 + 把最近记录滚进视口", () => {
    mockTrack();
    const p = props();
    render(<TrajectoryTimeline {...p} />);

    // x=550 → 域 3.3;最小选区宽 = 6/6 = 1 → [2.8, 3.8];最近的是 span [3,4)。
    fireEvent.pointerDown(track(), { clientX: 550, pointerId: 1, button: 0 });
    fireEvent.pointerUp(track(), { clientX: 550, pointerId: 1, button: 0 });

    const committed = (p.onRangeChange as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(committed.start).toBeCloseTo(2.8, 6);
    expect(committed.end).toBeCloseTo(3.8, 6);
    expect(p.onFocusRecord).toHaveBeenCalledWith(3);
    expect(p.onSelectRecord).not.toHaveBeenCalled();
  });

  it("双击清选区", () => {
    const p = props({ range: { start: 1, end: 2 } });
    render(<TrajectoryTimeline {...p} />);

    fireEvent.doubleClick(track());
    expect(p.onRangeChange).toHaveBeenCalledWith(null);
  });

  it("轨道聚焦时 Escape 清选区;没有选区时不回调", () => {
    const p = props({ range: { start: 1, end: 2 } });
    const { rerender } = render(<TrajectoryTimeline {...p} />);

    fireEvent.keyDown(track(), { key: "Escape" });
    expect(p.onRangeChange).toHaveBeenCalledWith(null);

    const q = props({ range: null });
    rerender(<TrajectoryTimeline {...q} />);
    fireEvent.keyDown(track(), { key: "Escape" });
    expect(q.onRangeChange).not.toHaveBeenCalled();
  });

  it("右键单击(没有拖动)清选区,并且不弹出浏览器右键菜单", () => {
    mockTrack();
    const p = props({ range: { start: 1, end: 2 } });
    render(<TrajectoryTimeline {...p} />);

    fireEvent.pointerDown(track(), { clientX: 300, pointerId: 1, button: 2 });
    fireEvent.pointerUp(track(), { clientX: 300, pointerId: 1, button: 2 });
    expect(p.onRangeChange).toHaveBeenCalledWith(null);

    const menu = fireEvent.contextMenu(track());
    // fireEvent 返回 false 表示事件被 preventDefault 了。
    expect(menu).toBe(false);
  });

  it("pointercancel 中止拖选:不提交区间,草稿选区也擦掉", () => {
    mockTrack();
    const p = props();
    render(<TrajectoryTimeline {...p} />);

    fireEvent.pointerDown(track(), { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(track(), { clientX: 400, pointerId: 1 });
    expect(screen.getByTestId("console-lane-range")).toHaveAttribute("data-dragging", "true");
    expect(screen.getByTestId("console-lane-range-edges")).toHaveAttribute("data-dragging", "true");

    fireEvent.pointerCancel(track(), { clientX: 400, pointerId: 1 });
    expect(screen.queryByTestId("console-lane-range")).not.toBeInTheDocument();
    expect(p.onRangeChange).not.toHaveBeenCalled();

    // 状态已清空 —— 之后落单的一次抬起不该补出区间。
    fireEvent.pointerUp(track(), { clientX: 500, pointerId: 1, button: 0 });
    expect(p.onRangeChange).not.toHaveBeenCalled();
  });
});

describe("TrajectoryTimeline · 联动 / 缩放 / 历史", () => {
  it("指针移到块上回抛 onHoverIndex,离开轨道回抛 null;hoveredIndex 给块打 data-hovered", () => {
    mockTrack();
    const p = props({ hoveredIndex: 2 });
    render(<TrajectoryTimeline {...p} />);

    expect(blocks().filter((b) => b.getAttribute("data-hovered") === "true").map((b) => b.getAttribute("data-index")))
      .toEqual(["2"]);

    // 空白处 → 一条跟随鼠标的竖线;移到块上就换成块自己的描边。
    fireEvent.pointerMove(track(), { clientX: 500, pointerId: 1 });
    expect(pct(screen.getByTestId("console-lane-hover-line"), "--traj-hover-left")).toBeCloseTo(50, 3);

    fireEvent.pointerMove(blockAt(4), { clientX: 700, pointerId: 1 });
    expect(p.onHoverIndex).toHaveBeenLastCalledWith(4);
    expect(screen.queryByTestId("console-lane-hover-line")).not.toBeInTheDocument();

    fireEvent.pointerLeave(track(), { clientX: 1001, pointerId: 1 });
    expect(p.onHoverIndex).toHaveBeenLastCalledWith(null);
  });

  it("向上滚轮放大:域投影宽度超过 100%,块整体被拉宽", () => {
    mockTrack();
    render(<TrajectoryTimeline {...props()} />);

    const lanes = blockAt(0).parentElement as HTMLElement;
    expect(pct(lanes, "--traj-domain-width")).toBeCloseTo(100, 3);

    fireEvent.wheel(screen.getByTestId("console-lane-strip"), { deltaY: -200, clientX: 500 });

    const zoomed = blockAt(0).parentElement as HTMLElement;
    expect(pct(zoomed, "--traj-domain-width")).toBeGreaterThan(100);
  });

  it("缩放态下右键按住拖动 = 平移视口(拖过了就不算右键单击)", () => {
    mockTrack();
    const p = props();
    render(<TrajectoryTimeline {...p} />);

    fireEvent.wheel(screen.getByTestId("console-lane-strip"), { deltaY: -200, clientX: 500 });
    const before = pct(blockAt(0).parentElement as HTMLElement, "--traj-domain-left");
    expect(before).toBeLessThan(0);

    fireEvent.pointerDown(track(), { clientX: 500, pointerId: 3, button: 2 });
    fireEvent.pointerMove(track(), { clientX: 600, pointerId: 3 });
    expect(track()).toHaveAttribute("data-panning", "true");
    // 内容跟着手指往右走 → 域左偏移变大(视口起点变小)。
    expect(pct(blockAt(0).parentElement as HTMLElement, "--traj-domain-left")).toBeGreaterThan(before);

    fireEvent.pointerUp(track(), { clientX: 600, pointerId: 3, button: 2 });
    expect(track()).not.toHaveAttribute("data-panning");
    expect(p.onRangeChange).not.toHaveBeenCalled();
  });

  it("hasEarlier 出「…」按钮,点击触发加载;loadingEarlier 时 aria-disabled 且不再回调", () => {
    const p = props({ hasEarlier: true });
    const { rerender } = render(<TrajectoryTimeline {...p} />);

    const button = screen.getByTestId("console-lane-earlier");
    expect(button).toHaveAttribute("aria-disabled", "false");
    fireEvent.click(button);
    expect(p.onLoadEarlier).toHaveBeenCalledTimes(1);

    const q = props({ hasEarlier: true, loadingEarlier: true });
    rerender(<TrajectoryTimeline {...q} />);
    const busy = screen.getByTestId("console-lane-earlier");
    expect(busy).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(busy);
    expect(q.onLoadEarlier).not.toHaveBeenCalled();
  });

  it("hasEarlier 在空态下也给出「…」入口", () => {
    render(<TrajectoryTimeline {...props({ model: null, records: [], hasEarlier: true })} />);

    expect(screen.getByTestId("console-lane-earlier")).toBeInTheDocument();
  });

  it("悬停块 500ms 后的提示含类型标签与总计;顺序模式不报钟点", async () => {
    render(<TrajectoryTimeline {...props()} />);

    fireEvent.mouseOver(blockAt(1));
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("ASSISTANT");
    expect(tip).toHaveTextContent("总计 400ms");
    // 顺序模式里块的横向位置跟真实时刻无关,报钟点会误导。
    expect(tip.textContent).not.toContain("→");
  });

  it("时长模式的提示多一行「起点 → 终点」钟点", async () => {
    render(<TrajectoryTimeline {...props({}, "duration")} />);

    expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration");
    fireEvent.mouseOver(blockAt(1));
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent(`${formatClock(1000)} → ${formatClock(1400)}`);
    expect(tip).toHaveTextContent("总计 400ms");
  });
});
