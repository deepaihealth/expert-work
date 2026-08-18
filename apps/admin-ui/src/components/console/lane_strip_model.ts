/**
 * 泳道 v2 数据层 —— 把一轮的轨迹行(`api/trajectory_rows.ts` 的
 * `TrajectoryRow`)投影到三条泳道(用户 / 模型 / 工具)的两种域上:
 *
 * - `sequence`(默认):每行等宽,按发生顺序排,并行工具自然分开;
 * - `duration`:按 `api/gantt_timeline.ts` 的真实起止毫秒排,没时序的行退成点块。
 *
 * 纯函数,不渲染、不持状态。交互模型参照 deepseek-harness
 * `ui-trajectory/src/client/timeline.ts` 的双投影思路(顺序域 `[i, i+1)` /
 * 时长域 ms / 区间相交求焦点行),字段与实现自成一套。见 spec §八.7。
 */
import { buildGanttRows, type GanttModel } from "../../api/gantt_timeline";
import type { SseEvent } from "../../api/sessions";
import { serverMsOf } from "../../api/sse_id";
import { resolveGanttKey, type TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";

export type Lane = "user" | "model" | "tools";
export type LaneMode = "sequence" | "duration";

/** 行种类 → 泳道。显式 `Record<TrajectoryRow["kind"], Lane>` 标注就是编译期
 *  穷尽闸:`TrajectoryRow` 新增一个 kind 而这里没跟上,`pnpm typecheck` 直接红。 */
export const LANE_OF_KIND: Record<TrajectoryRow["kind"], Lane> = {
  user: "user", plan: "user", memory: "user", reflect: "user",
  compaction: "user", retry: "user", error: "user", approval: "user", guard: "user", gap: "user",
  think: "model", assistant: "model",
  tool: "tools", subagent: "tools",
};

export interface LaneBlock {
  key: string;            // row.id
  rowId: string;
  rowIndex: number;       // 1-based,与行表 # 一致
  lane: Lane;
  kind: TrajectoryRow["kind"];
  /** 投影域里的起止:sequence 模式 = [i, i+1);duration 模式 = ms。 */
  start: number; end: number;
  hasError: boolean;
  live: boolean;          // 运行中且 durationMs 为 null 的尾块
}

export interface LaneTick { at: number; label: string }

export interface LaneProjection {
  mode: LaneMode;
  blocks: LaneBlock[];
  /** 域长度:sequence = rows.length;duration = totalMs(运行中随 nowMs 生长)。 */
  total: number;
  ticks: LaneTick[];
  degraded: boolean;      // duration 模式且 gantt 无时序时 true(此时 blocks 退化成 sequence 排布)
}

/** 顺序刻度密度:每 `ceil(N / SEQ_TICK_SLOTS)` 行一个 `#k`。 */
const SEQ_TICK_SLOTS = 6;
/** 时长刻度:0 / 25% / 50% / 75% / 100%。 */
const DURATION_TICK_FRACTIONS = [0, 0.25, 0.5, 0.75, 1] as const;

/** 「运行中的尾块」—— 该轮还在跑、且末行还没有自己的耗时。 */
function isLiveTail(rows: readonly TrajectoryRow[], i: number, running: boolean): boolean {
  return running && i === rows.length - 1 && rows[i].durationMs === null;
}

function sequenceTicks(n: number): LaneTick[] {
  if (n === 0) return [];
  const step = Math.max(1, Math.ceil(n / SEQ_TICK_SLOTS));
  const ks: number[] = [];
  for (let k = 1; k <= n; k += step) ks.push(k);
  if (ks[ks.length - 1] !== n) ks.push(n);
  return ks.map((k) => ({ at: k - 1, label: `#${k}` }));
}

/** 顺序投影 —— 每行一个等宽块 `[i, i+1)`,域长 = 行数。永远画得出。 */
function sequenceProjection(rows: readonly TrajectoryRow[], running: boolean): LaneProjection {
  const blocks: LaneBlock[] = rows.map((row, i) => ({
    key: row.id,
    rowId: row.id,
    rowIndex: i + 1,
    lane: LANE_OF_KIND[row.kind],
    kind: row.kind,
    start: i,
    end: i + 1,
    hasError: row.status === "error",
    live: isLiveTail(rows, i, running),
  }));
  return { mode: "sequence", blocks, total: rows.length, ticks: sequenceTicks(rows.length), degraded: false };
}

/** Copied verbatim from `components/turn/TurnCard.tsx:151-162` (growing-bar
 *  calibration anchor) — NOT imported, since that module is component-layer
 *  and this file must stay pure. See that file's doc comment for the
 *  client/server clock-skew rationale. */
function lastKnownFrame(
  events: readonly SseEvent[],
): { serverMs: number; receivedAtMs: number } | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const serverMs = serverMsOf(events[i].id);
    const receivedAtMs = Date.parse(events[i].receivedAt);
    if (serverMs !== null && Number.isFinite(receivedAtMs)) {
      return { serverMs, receivedAtMs };
    }
  }
  return null;
}

function durationTicks(total: number): LaneTick[] {
  return DURATION_TICK_FRACTIONS.map((f) => {
    const at = Math.round(total * f);
    return { at, label: fmtDuration(at) };
  });
}

/** gantt 相对轴的原点(绝对服务端 ms)—— 反解自任意一条「既有 gantt 时序、
 *  又有绝对 `serverMs`」的行:gantt 里 `relEnd = serverMs - t0`,而
 *  `relEnd = startMs + durationMs`。只认 think / tool 两种行,因为它们与
 *  gantt 的 `item-<seq>` / `tool-<id>` 是一对一的(plan 行可能由 planner 与
 *  update_plan 两个不同时刻的单元合并而来,拿来反解会偏)。没有可配对的行
 *  → null,此时「只有 serverMs、没有 gantt 时序」的行不画。 */
function ganttT0(rows: readonly TrajectoryRow[], gantt: GanttModel): number | null {
  const byKey = new Map(gantt.rows.map((g) => [g.key, g]));
  for (const row of rows) {
    if (row.serverMs === null) continue;
    const key = row.kind === "think" ? `item-${row.seq}` : row.kind === "tool" ? `tool-${row.entry.id}` : null;
    if (key === null) continue;
    const g = byKey.get(key);
    if (g === undefined || g.durationMs === null) continue;
    return row.serverMs - (g.startMs + g.durationMs);
  }
  return null;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), hi);
}

/** 一条行在时长域里的起止:user 钉 0、assistant 钉 total,有 gantt 时序的用
 *  它,其余按绝对 `serverMs` 换算成点块;都没有 → null(不画)。 */
function durationSpanOf(
  row: TrajectoryRow,
  timed: ReadonlyMap<string, { start: number; end: number }>,
  t0: number | null,
  total: number,
): { start: number; end: number } | null {
  if (row.kind === "user") return { start: 0, end: 0 };
  if (row.kind === "assistant") return { start: total, end: total };
  const hit = timed.get(row.id);
  if (hit !== undefined) return hit;
  if (row.serverMs === null || t0 === null) return null;
  const at = row.serverMs - t0;
  return { start: at, end: at };
}

/** 时长投影 —— gantt 时序为准。gantt 降级(缺服务端时戳)或域长为 0 时整体
 *  退成顺序排布并标 `degraded`,保证永远画得出。 */
function durationProjection(
  rows: readonly TrajectoryRow[],
  events: readonly SseEvent[],
  opts: { running: boolean; nowMs: number },
): LaneProjection {
  const gantt = buildGanttRows(events, { settled: !opts.running });

  // 运行中按 PR-A `laneModelOf` 的算法生长到「现在」:用最后一条带合法 id 的
  // 帧把客户端 `nowMs` 校准到服务端时钟。
  let total = gantt.totalMs;
  if (opts.running) {
    const last = lastKnownFrame(events);
    if (last !== null) {
      const nowServerMs = last.serverMs + (opts.nowMs - last.receivedAtMs);
      total = gantt.totalMs + Math.max(0, nowServerMs - last.serverMs);
    }
  }

  if (gantt.degraded || total <= 0) {
    return { ...sequenceProjection(rows, opts.running), mode: "duration", degraded: true };
  }

  // 一条轨迹行可能对应多条 gantt 行(子代理行 = 该 worker 的每一步),取并集。
  const timed = new Map<string, { start: number; end: number }>();
  for (const g of gantt.rows) {
    const rowId = resolveGanttKey(rows, g.key);
    if (rowId === null) continue;
    const span = { start: g.startMs, end: g.startMs + (g.durationMs ?? 0) };
    const prev = timed.get(rowId);
    timed.set(
      rowId,
      prev === undefined ? span : { start: Math.min(prev.start, span.start), end: Math.max(prev.end, span.end) },
    );
  }

  const t0 = ganttT0(rows, gantt);
  const blocks: LaneBlock[] = [];
  rows.forEach((row, i) => {
    const span = durationSpanOf(row, timed, t0, total);
    if (span === null) return;
    const live = isLiveTail(rows, i, opts.running);
    const start = clamp(span.start, 0, total);
    blocks.push({
      key: row.id,
      rowId: row.id,
      rowIndex: i + 1,
      lane: LANE_OF_KIND[row.kind],
      kind: row.kind,
      start,
      // 运行中的尾块一直长到域末端(呼吸块)。
      end: clamp(live ? total : span.end, start, total),
      hasError: row.status === "error",
      live,
    });
  });

  return { mode: "duration", blocks, total, ticks: durationTicks(total), degraded: false };
}

export function laneProjection(
  rows: readonly TrajectoryRow[], events: readonly SseEvent[],
  opts: { mode: LaneMode; running: boolean; nowMs: number },
): LaneProjection {
  if (rows.length === 0) return { mode: opts.mode, blocks: [], total: 0, ticks: [], degraded: false };
  return opts.mode === "sequence"
    ? sequenceProjection(rows, opts.running)
    : durationProjection(rows, events, opts);
}

/** 拖选:域坐标 [a,b] → 与之相交的行 index 区间(1-based 闭区间);无相交 → null。 */
export function rangeToRowSpan(p: LaneProjection, a: number, b: number): { from: number; to: number } | null {
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  // 有宽度的块按开区间相交(拖在第 3 块内部只选第 3 块,不蹭到邻块的边界);
  // 点块(user / marker / 未完成单元)按闭区间,否则永远选不中。
  const hits = p.blocks.filter((bl) =>
    bl.end > bl.start ? bl.start < hi && bl.end > lo : bl.start <= hi && bl.start >= lo,
  );
  if (hits.length === 0) return null;
  return {
    from: Math.min(...hits.map((h) => h.rowIndex)),
    to: Math.max(...hits.map((h) => h.rowIndex)),
  };
}
