/**
 * ledger_timeline —— 账本的时间轴投影模型(顺序 / 时长两种域)。纯函数,不
 * 渲染、不持状态。压空档算法与滚轮缩放 / 选中挪视口的交互参照 deepseek-harness
 * ui-trajectory(MIT)重写,字段与实现自成一套。见 spec §九。
 */
import type { TrajectoryRow } from "../../api/trajectory_rows";
import type { LedgerLane } from "./ledger_types";

export type TimelineMode = "sequence" | "duration";

export interface TimeRange {
  start: number;
  end: number;
}

/** 只依赖这几个字段,`LedgerRecord` 结构上满足。 */
export interface TimelineSpanInput {
  index: number;
  lane: LedgerLane;
  kind: TrajectoryRow["kind"];
  isError: boolean;
  running: boolean;
  turnSeq: number;
  turnStart: boolean;
  startedAt: number | null;
  endedAt: number | null;
}

export interface TimelineSpan extends TimeRange {
  index: number;
  lane: LedgerLane;
  kind: TrajectoryRow["kind"];
  isError: boolean;
  running: boolean;
}

export interface TimelineModel extends TimeRange {
  mode: TimelineMode;
  spans: TimelineSpan[];
  /** 每轮首条记录的 time(首轮也含,渲染时过滤 time > start)。 */
  turnBoundaries: { turnSeq: number; time: number }[];
  /** 要求 duration 但有记录没时序 → 按 sequence 排布并置 true。 */
  degraded: boolean;
}

const MINIMUM_ZOOM_SEQUENCE = 4;
const MINIMUM_ZOOM_DURATION = 20;
const WHEEL_ZOOM_FACTOR = 0.0015;
const FULL_VIEW_THRESHOLD = 0.999;

function spanOf(record: TimelineSpanInput, start: number, end: number): TimelineSpan {
  return { index: record.index, lane: record.lane, kind: record.kind, isError: record.isError, running: record.running, start, end };
}

/** 顺序排布:`spans[i] = [i, i+1)`;边界取每个 `turnStart` 记录的位置 i。 */
function sequenceLayout(
  records: readonly TimelineSpanInput[],
): { spans: TimelineSpan[]; turnBoundaries: { turnSeq: number; time: number }[] } {
  const spans = records.map((record, i) => spanOf(record, i, i + 1));
  const turnBoundaries = records
    .map((record, i) => ({ record, i }))
    .filter(({ record }) => record.turnStart)
    .map(({ record, i }) => ({ turnSeq: record.turnSeq, time: i }));
  return { spans, turnBoundaries };
}

/** 时长排布:原始 span 按 start(打平打并)排序压空档(deepseek
 *  `compressIdle`),按原始顺序输出;边界取每轮压缩后的最小 start。 */
function durationLayout(
  records: readonly TimelineSpanInput[],
): { spans: TimelineSpan[]; turnBoundaries: { turnSeq: number; time: number }[] } {
  const raw = records.map((record) => ({
    record,
    start: record.startedAt as number,
    end: record.endedAt ?? (record.startedAt as number),
  }));
  const sorted = [...raw].sort((a, b) => a.start - b.start || a.end - b.end);
  const offsetOf = new Map<(typeof raw)[number], number>();
  let removedIdle = 0;
  let coveredUntil: number | null = null;
  for (const item of sorted) {
    if (coveredUntil !== null && item.start > coveredUntil) {
      removedIdle += item.start - coveredUntil;
    }
    offsetOf.set(item, removedIdle);
    coveredUntil = coveredUntil === null ? item.end : Math.max(coveredUntil, item.end);
  }

  const spans = raw.map((item) => {
    const offset = offsetOf.get(item) ?? 0;
    return spanOf(item.record, item.start - offset, item.end - offset);
  });

  const turnBoundaries: { turnSeq: number; time: number }[] = [];
  const boundaryIndexOf = new Map<number, number>();
  spans.forEach((span, i) => {
    const turnSeq = records[i].turnSeq;
    const existingAt = boundaryIndexOf.get(turnSeq);
    if (existingAt === undefined) {
      boundaryIndexOf.set(turnSeq, turnBoundaries.length);
      turnBoundaries.push({ turnSeq, time: span.start });
    } else if (span.start < turnBoundaries[existingAt].time) {
      turnBoundaries[existingAt] = { turnSeq, time: span.start };
    }
  });

  return { spans, turnBoundaries };
}

/** 投影账本记录为时间轴模型;无记录 → `null`。 */
export function deriveTimeline(records: readonly TimelineSpanInput[], mode: TimelineMode): TimelineModel | null {
  if (records.length === 0) return null;

  if (mode === "sequence") {
    const { spans, turnBoundaries } = sequenceLayout(records);
    return { mode: "sequence", spans, turnBoundaries, start: 0, end: records.length, degraded: false };
  }

  if (records.some((record) => record.startedAt === null)) {
    const { spans, turnBoundaries } = sequenceLayout(records);
    return { mode: "duration", spans, turnBoundaries, start: 0, end: records.length, degraded: true };
  }

  const { spans, turnBoundaries } = durationLayout(records);
  const start = Math.min(...spans.map((span) => span.start));
  const end = Math.max(...spans.map((span) => span.end));
  return { mode: "duration", spans, turnBoundaries, start, end, degraded: false };
}

/** 与选区有交集的记录(闭区间,边界上的点块也算)。 */
export function focusIndexes(model: TimelineModel, range: TimeRange): ReadonlySet<number> {
  return new Set(
    model.spans.filter((span) => span.start <= range.end && span.end >= range.start).map((span) => span.index),
  );
}

function distanceToPoint(span: TimeRange, time: number): number {
  if (time < span.start) return span.start - time;
  if (time > span.end) return time - span.end;
  return 0;
}

/** 距 `time` 最近的记录(距离到 `[start,end]`,命中区间内距离为 0);无 span → `null`。 */
export function nearestSpan(model: TimelineModel, time: number): TimelineSpan | null {
  if (model.spans.length === 0) return null;
  return model.spans.reduce((nearest, span) =>
    (distanceToPoint(span, time) < distanceToPoint(nearest, time) ? span : nearest));
}

/** 「一条记录宽」的最小选区时长。 */
export function minimumSelection(model: TimelineModel, domainDuration: number): number {
  return Math.min(domainDuration, (model.end - model.start) / model.spans.length);
}

export function orderedRange(a: number, b: number): TimeRange {
  return a <= b ? { start: a, end: b } : { start: b, end: a };
}

export function centeredRange(center: number, width: number, min: number, max: number): TimeRange {
  const clampedWidth = Math.min(max - min, Math.max(0, width));
  const start = Math.min(Math.max(center - clampedWidth / 2, min), max - clampedWidth);
  return { start, end: start + clampedWidth };
}

export function clampFraction(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/** 滚轮缩放:以 `anchorFraction` 为锚,视口时长按 `exp(deltaY*0.0015)` 缩放
 *  (`viewport===null` 视作全景);最小宽度顺序模式 4 条记录 / 时长模式
 *  20ms;缩到 ≥ 全景 99.9% → 回全景(`null`)。 */
export function zoomViewport(
  model: TimelineModel,
  viewport: TimeRange | null,
  anchorFraction: number,
  deltaY: number,
): TimeRange | null {
  const fullDuration = Math.max(1, model.end - model.start);
  const domainDuration =
    viewport === null ? fullDuration : Math.min(fullDuration, Math.max(1, viewport.end - viewport.start));
  const domainStart =
    viewport === null ? model.start : Math.min(Math.max(viewport.start, model.start), model.end - domainDuration);
  // 退化态(`degraded`)布局已经是等宽顺序块,域单位是记录数不是毫秒 ——
  // 最小宽度要按有效模式(sequence)算,不能沿用 `mode` 字面上仍报的 duration。
  const effectiveMode = model.degraded ? "sequence" : model.mode;
  const minimumWidth = Math.min(
    effectiveMode === "sequence" ? MINIMUM_ZOOM_SEQUENCE : MINIMUM_ZOOM_DURATION,
    fullDuration,
  );
  const nextDuration = Math.min(
    fullDuration,
    Math.max(minimumWidth, domainDuration * Math.exp(deltaY * WHEEL_ZOOM_FACTOR)),
  );
  if (nextDuration >= fullDuration * FULL_VIEW_THRESHOLD) return null;
  const anchorTime = domainStart + anchorFraction * domainDuration;
  const nextStart = Math.min(
    Math.max(anchorTime - anchorFraction * nextDuration, model.start),
    model.end - nextDuration,
  );
  return { start: nextStart, end: nextStart + nextDuration };
}

/** 平移:视口整体挪 `deltaDomain`,夹在 `[model.start, model.end]`。 */
export function panViewport(model: TimelineModel, viewport: TimeRange, deltaDomain: number): TimeRange {
  const width = viewport.end - viewport.start;
  const start = Math.min(
    Math.max(viewport.start + deltaDomain, model.start),
    Math.max(model.start, model.end - width),
  );
  return { start, end: start + width };
}

/** 选中记录在视口外时挪到最小距离能看见它;全景(`viewport===null`)不用挪。 */
export function revealInViewport(model: TimelineModel, viewport: TimeRange | null, index: number): TimeRange | null {
  if (viewport === null) return null;
  const span = model.spans.find((s) => s.index === index);
  if (span === undefined) return viewport;
  if (span.end > viewport.start && span.start < viewport.end) return viewport;
  const width = viewport.end - viewport.start;
  const desiredStart = span.end <= viewport.start ? span.start : span.end - width;
  const start = Math.min(Math.max(desiredStart, model.start), Math.max(model.start, model.end - width));
  return { start, end: start + width };
}

/** `HH:MM:SS.mmm`(本地时区)。 */
export function formatClock(ms: number): string {
  const date = new Date(ms);
  const pad2 = (n: number) => String(n).padStart(2, "0");
  const pad3 = (n: number) => String(n).padStart(3, "0");
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}.${pad3(date.getMilliseconds())}`;
}
