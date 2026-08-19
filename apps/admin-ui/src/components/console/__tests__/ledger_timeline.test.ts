/**
 * ledger_timeline 测试(PR-A.2 Task 3)—— 顺序 / 时长两种投影、选区求交、
 * 最近记录、滚轮缩放、平移、挪视口显影、钟点格式化。
 */
import { describe, expect, it } from "vitest";

import {
  centeredRange, clampFraction, deriveTimeline, focusIndexes, formatClock, minimumSelection,
  nearestSpan, orderedRange, panViewport, revealInViewport, zoomViewport,
  type TimelineModel, type TimelineSpanInput,
} from "../ledger_timeline";

/**
 * 9 条 · 3 轮 fixture:
 * - 轮 0(user + assistant + 两条并行 tool,时间上交叠 —— r3 早于 r2 开始但
 *   数组顺序 r2 在前,用来验证压空档按 start 排序而非按数组顺序)
 * - 轮 1(assistant + tool,与轮 0 之间隔 1300ms 空档)
 * - 轮 2(assistant + tool + 运行中的尾条 assistant,`endedAt: null`;与轮 1
 *   之间隔 500ms 空档)
 */
function nineRecords(): TimelineSpanInput[] {
  return [
    { index: 0, lane: 0, kind: "user", isError: false, running: false, turnSeq: 0, turnStart: true, startedAt: 1000, endedAt: 1000 },
    { index: 1, lane: 1, kind: "assistant", isError: false, running: false, turnSeq: 0, turnStart: false, startedAt: 1000, endedAt: 1400 },
    { index: 2, lane: 2, kind: "tool", isError: false, running: false, turnSeq: 0, turnStart: false, startedAt: 1450, endedAt: 1650 },
    { index: 3, lane: 2, kind: "tool", isError: true, running: false, turnSeq: 0, turnStart: false, startedAt: 1400, endedAt: 1700 },
    { index: 4, lane: 1, kind: "assistant", isError: false, running: false, turnSeq: 1, turnStart: true, startedAt: 3000, endedAt: 3200 },
    { index: 5, lane: 2, kind: "tool", isError: false, running: false, turnSeq: 1, turnStart: false, startedAt: 3200, endedAt: 3500 },
    { index: 6, lane: 1, kind: "assistant", isError: false, running: false, turnSeq: 2, turnStart: true, startedAt: 4000, endedAt: 4300 },
    { index: 7, lane: 2, kind: "tool", isError: false, running: false, turnSeq: 2, turnStart: false, startedAt: 4300, endedAt: 4600 },
    { index: 8, lane: 1, kind: "assistant", isError: false, running: true, turnSeq: 2, turnStart: false, startedAt: 4600, endedAt: null },
  ];
}

describe("deriveTimeline · sequence", () => {
  it("sequence: unit spans, boundaries at each turnStart, start 0 end n", () => {
    const model = deriveTimeline(nineRecords(), "sequence");
    expect(model).not.toBeNull();
    expect(model?.mode).toBe("sequence");
    expect(model?.degraded).toBe(false);
    expect(model?.start).toBe(0);
    expect(model?.end).toBe(9);
    expect(model?.spans.map((s) => [s.start, s.end])).toEqual(
      [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9]],
    );
    expect(model?.spans.map((s) => s.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8]);
    expect(model?.turnBoundaries).toEqual([
      { turnSeq: 0, time: 0 }, { turnSeq: 1, time: 4 }, { turnSeq: 2, time: 6 },
    ]);
  });

  it("deriveTimeline returns null when there are no records", () => {
    expect(deriveTimeline([], "sequence")).toBeNull();
    expect(deriveTimeline([], "duration")).toBeNull();
  });
});

describe("deriveTimeline · duration", () => {
  it("duration: spans use absolute times, idle gaps between spans are removed, boundaries at each turn's first start", () => {
    const model = deriveTimeline(nineRecords(), "duration");
    expect(model).not.toBeNull();
    expect(model?.mode).toBe("duration");
    expect(model?.degraded).toBe(false);
    // 空档 1: turn0→turn1 = 1700..3000 (1300ms); 空档 2: turn1→turn2 = 3500..4000 (500ms) 都被压掉。
    expect(model?.start).toBe(1000);
    expect(model?.end).toBe(2800);

    const byIndex = new Map(model?.spans.map((s) => [s.index, s]));
    expect(byIndex.get(0)).toMatchObject({ start: 1000, end: 1000 });
    expect(byIndex.get(1)).toMatchObject({ start: 1000, end: 1400 });
    expect(byIndex.get(4)).toMatchObject({ start: 1700, end: 1900 });
    expect(byIndex.get(5)).toMatchObject({ start: 1900, end: 2200 });
    expect(byIndex.get(6)).toMatchObject({ start: 2200, end: 2500 });
    expect(byIndex.get(7)).toMatchObject({ start: 2500, end: 2800 });
    // 运行中、endedAt=null 的尾条按 `startedAt` 收成点块。
    expect(byIndex.get(8)).toMatchObject({ start: 2800, end: 2800, running: true });

    expect(model?.turnBoundaries).toEqual([
      { turnSeq: 0, time: 1000 }, { turnSeq: 1, time: 1700 }, { turnSeq: 2, time: 2200 },
    ]);
  });

  it("duration: overlapping spans (parallel tools) keep their overlap after compression", () => {
    const model = deriveTimeline(nineRecords(), "duration");
    const byIndex = new Map(model?.spans.map((s) => [s.index, s]));
    const r2 = byIndex.get(2)!; // 数组里在 r3 之前,但原始 startedAt 更晚
    const r3 = byIndex.get(3)!;
    expect(r2).toMatchObject({ start: 1450, end: 1650, isError: false });
    expect(r3).toMatchObject({ start: 1400, end: 1700, isError: true });
    // 压缩前 r3 覆盖 r2(1400..1700 ⊇ 1450..1650);压缩不改变相对关系。
    expect(r3.start <= r2.start && r3.end >= r2.end).toBe(true);
    expect(r3.start < r2.end && r2.start < r3.end).toBe(true); // 仍然交叠
  });

  it("duration with a null startedAt → sequence layout + degraded=true", () => {
    const records = nineRecords();
    records[5] = { ...records[5], startedAt: null };
    const model = deriveTimeline(records, "duration");
    expect(model).not.toBeNull();
    expect(model?.mode).toBe("duration"); // mode 仍报 duration
    expect(model?.degraded).toBe(true);
    expect(model?.start).toBe(0);
    expect(model?.end).toBe(9);
    expect(model?.spans.map((s) => [s.start, s.end])).toEqual(
      [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9]],
    );
    expect(model?.turnBoundaries).toEqual([
      { turnSeq: 0, time: 0 }, { turnSeq: 1, time: 4 }, { turnSeq: 2, time: 6 },
    ]);
  });
});

describe("focusIndexes", () => {
  it("returns spans intersecting the inclusive range (a point span on the boundary counts)", () => {
    const model = deriveTimeline(nineRecords(), "duration") as TimelineModel;
    // index 6 = [2200,2500] 够不到 2600 的下界; index 7 = [2500,2800] 与
    // [2600,2800] 交叠;index 8 = [2800,2800] 恰好落在选区右边界上,也算命中。
    expect(focusIndexes(model, { start: 2600, end: 2800 })).toEqual(new Set([7, 8]));
    expect(focusIndexes(model, { start: 0, end: 100 })).toEqual(new Set([]));
    expect(focusIndexes(model, { start: 1000, end: 2800 })).toEqual(new Set([0, 1, 2, 3, 4, 5, 6, 7, 8]));
  });
});

describe("nearestSpan", () => {
  it("picks the closest by distance to [start,end]", () => {
    const model = deriveTimeline(nineRecords(), "duration") as TimelineModel;
    // 1420 落在 index 3([1400,1700])内部(距离 0),不落在 index 1([1000,1400])
    // 或 index 2([1450,1650])里 —— 三者互不歧义。
    expect(nearestSpan(model, 1420)?.index).toBe(3);
    // 6000 远超全部 span,最近的是全时间轴末尾的两个点块之一(index 7 先达到
    // 并列最小距离,reduce 保留先遇到的候选)。
    expect(nearestSpan(model, 6000)?.index).toBe(7);
  });

  it("returns null when the model has no spans", () => {
    // deriveTimeline 保证有记录就至少一个 span,这里直接构一个空 model 测防御分支。
    const empty: TimelineModel = { mode: "sequence", spans: [], turnBoundaries: [], start: 0, end: 0, degraded: false };
    expect(nearestSpan(empty, 5)).toBeNull();
  });
});

describe("minimumSelection", () => {
  it("= min(domain, full/spans)", () => {
    const model = deriveTimeline(nineRecords(), "sequence") as TimelineModel;
    // full = 9, spans.length = 9 → full/spans = 1
    expect(minimumSelection(model, 5)).toBe(1);
    expect(minimumSelection(model, 0.3)).toBeCloseTo(0.3, 10);
  });
});

describe("orderedRange", () => {
  it("orders its two arguments ascending regardless of input order", () => {
    expect(orderedRange(5, 2)).toEqual({ start: 2, end: 5 });
    expect(orderedRange(2, 5)).toEqual({ start: 2, end: 5 });
    expect(orderedRange(3, 3)).toEqual({ start: 3, end: 3 });
  });
});

describe("centeredRange", () => {
  it("clamps to [min,max] keeping width", () => {
    expect(centeredRange(5, 4, 0, 10)).toEqual({ start: 3, end: 7 });
    // 贴左边界:宽度不变,整体挪到 min。
    expect(centeredRange(1, 4, 0, 10)).toEqual({ start: 0, end: 4 });
    // 贴右边界:宽度不变,整体挪到 max。
    expect(centeredRange(9, 4, 0, 10)).toEqual({ start: 6, end: 10 });
    // 请求宽度超过整个定义域 → 收窄到定义域宽度。
    expect(centeredRange(5, 20, 0, 10)).toEqual({ start: 0, end: 10 });
  });
});

describe("clampFraction", () => {
  it("clamps into [0,1]", () => {
    expect(clampFraction(-0.2)).toBe(0);
    expect(clampFraction(1.5)).toBe(1);
    expect(clampFraction(0.42)).toBeCloseTo(0.42, 10);
  });
});

describe("zoomViewport", () => {
  it("wheel-in zooms around the anchor, keeping the anchor at the same fraction of the new viewport", () => {
    const model = deriveTimeline(nineRecords(), "sequence") as TimelineModel; // full [0,9]
    const anchorFraction = 0.5;
    const viewport = zoomViewport(model, null, anchorFraction, -500);
    expect(viewport).not.toBeNull();
    const duration = viewport!.end - viewport!.start;
    const expectedDuration = 9 * Math.exp(-500 * 0.0015);
    expect(duration).toBeCloseTo(expectedDuration, 6);
    expect(duration).toBeLessThan(9);
    const anchorTime = 0 + anchorFraction * 9;
    expect((anchorTime - viewport!.start) / duration).toBeCloseTo(anchorFraction, 6);
  });

  it("zooming in past the minimum width clamps to it (4 for sequence mode)", () => {
    const model = deriveTimeline(nineRecords(), "sequence") as TimelineModel;
    const viewport = zoomViewport(model, null, 0.5, -100_000);
    expect(viewport).not.toBeNull();
    expect(viewport!.end - viewport!.start).toBe(4);
  });

  it("duration mode's minimum width is 20, not 4", () => {
    const model = deriveTimeline(nineRecords(), "duration") as TimelineModel; // full = 1800
    const viewport = zoomViewport(model, null, 0.5, -100_000);
    expect(viewport).not.toBeNull();
    expect(viewport!.end - viewport!.start).toBe(20);
  });

  it("zooming out past 99.9% of the full domain returns null (back to overview)", () => {
    const model = deriveTimeline(nineRecords(), "sequence") as TimelineModel;
    const viewport = zoomViewport(model, { start: 2, end: 6 }, 0.5, 100_000);
    expect(viewport).toBeNull();
  });
});

describe("panViewport", () => {
  it("clamps to the model bounds", () => {
    const model = deriveTimeline(nineRecords(), "duration") as TimelineModel; // bounds [1000,2800]
    const viewport = { start: 1500, end: 1900 }; // width 400
    expect(panViewport(model, viewport, 2000)).toEqual({ start: 2400, end: 2800 });
    expect(panViewport(model, viewport, -2000)).toEqual({ start: 1000, end: 1400 });
    // 域内平移原样跟随。
    expect(panViewport(model, viewport, 100)).toEqual({ start: 1600, end: 2000 });
  });
});

describe("revealInViewport", () => {
  it("keeps the viewport when the span is visible, else moves the minimal distance", () => {
    const model = deriveTimeline(nineRecords(), "duration") as TimelineModel; // bounds [1000,2800]

    // 全景(viewport === null)不用挪。
    expect(revealInViewport(model, null, 0)).toBeNull();

    // index 5 = [1900,2200] 与视口 [1900,2300] 相交 → 原样返回。
    const visible = { start: 1900, end: 2300 };
    expect(revealInViewport(model, visible, 5)).toEqual(visible);

    // index 4 = [1700,1900] 在视口左侧 → 新视口 start = span.start,宽度不变。
    // (刻意不选贴着 model.start 的 span,免得 clamp 掩盖了公式选错分支的变异。)
    expect(revealInViewport(model, visible, 4)).toEqual({ start: 1700, end: 2100 });

    // index 6 = [2200,2500] 在视口右侧 → 新视口 end = span.end,宽度不变。
    // (同上,不选贴着 model.end 的 span。)
    const left = { start: 1000, end: 1400 };
    expect(revealInViewport(model, left, 6)).toEqual({ start: 2100, end: 2500 });
  });
});

describe("formatClock", () => {
  it("renders HH:MM:SS.mmm in the local timezone", () => {
    const d = new Date(2026, 0, 15, 9, 5, 3, 7);
    expect(formatClock(d.getTime())).toBe("09:05:03.007");
    const midnight = new Date(2026, 0, 15, 0, 0, 0, 0);
    expect(formatClock(midnight.getTime())).toBe("00:00:00.000");
  });
});
