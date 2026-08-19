/**
 * 账本的绝对时序(spec §九「概览时间轴 · 时长投影」,PR-A.2 Task 2)——
 * 把一轮的轨迹行摊到**服务端绝对毫秒**轴上,好让整个会话的多轮记录能排在
 * 同一条轴上(PR-A.1 的泳道只在一轮内部排,用的是 gantt 的相对轴)。
 *
 * 绝对时刻只来自 SSE 帧 id 的毫秒段(`serverMsOf` → `GanttModel.originMs`),
 * 绝不用 `receivedAt` —— 回放的轮那个字段全挤在回放那一瞬间。
 *
 * 从 `ledger.ts` 拆出来是为了守住单文件 400 行的上限;两个函数都由
 * `ledger.ts` 原样再导出,调用方按 `ledger` 引就行。
 */
import { buildGanttRows } from "../../api/gantt_timeline";
import type { SseEvent } from "../../api/sessions";
import { serverMsOf } from "../../api/sse_id";
import { resolveGanttKey, type TrajectoryRow } from "../../api/trajectory_rows";

export interface AbsoluteSpan { start: number; end: number }

/** 每轮的结果按 `events` 数组引用缓存(PR-A.2 终审 I1)。运行中账本每一次 rAF
 *  flush 都重建一遍(`liveByStep` 换引用),而历史轮的 `events` 引用一整个会话
 *  都不变 —— 没有这层缓存,窗口里 20 轮历史每帧要白跑 20 次 gantt。
 *
 *  只认 `events` 引用是安全的:同一份 events 下 `rows` 唯一的变数是尾部追加的
 *  live 合成行,而它们的 `seq` 恒 -1、`serverMs` 恒 null、工具 entry id 带
 *  `live-` 前缀 —— `resolveGanttKey` 与点块那一轮都收不下,对结果没有贡献。
 *  `fallbackStart` 是唯一不由 events 决定的入参(USER 行兜底起点),所以它
 *  一并进键:两轮共用一个空 `events` 时才不会互相串起点。 */
const SPAN_CACHE = new WeakMap<
  readonly SseEvent[],
  { fallbackStart: number | null; spans: ReadonlyMap<string, AbsoluteSpan> | null }
>();

/** 一轮的行 → 绝对起止(服务端 ms)。**返回的 Map 是缓存实例,调用方只读。**
 *
 *  - gantt `degraded`(有帧 id 缺失 / 畸形,整轮时序不可信)→ 整个返回 `null`;
 *  - 有 gantt 时序的行取 `originMs + startMs` 起、`+ durationMs` 止(一行可能
 *    对应多条 gantt 行 —— 子代理行 = 该 worker 的每一步 —— 取并集);
 *  - 没有 gantt 命中但有 `serverMs` 的行(标记行:gantt 把它们放进 `markers`
 *    而不是 `rows`)→ 点块 `[serverMs, serverMs]`;
 *  - `user` 行 → 本轮最早起点;一条有时序的行都没有时退到 `fallbackStart`
 *    (它也是 `null` 就不给 user 行落时序)。 */
export function absoluteSpans(
  rows: readonly TrajectoryRow[],
  events: readonly SseEvent[],
  fallbackStart: number | null,
): ReadonlyMap<string, AbsoluteSpan> | null {
  const cached = SPAN_CACHE.get(events);
  if (cached !== undefined && cached.fallbackStart === fallbackStart) return cached.spans;
  const spans = computeSpans(rows, events, fallbackStart);
  SPAN_CACHE.set(events, { fallbackStart, spans });
  return spans;
}

function computeSpans(
  rows: readonly TrajectoryRow[],
  events: readonly SseEvent[],
  fallbackStart: number | null,
): Map<string, AbsoluteSpan> | null {
  const gantt = buildGanttRows(events);
  if (gantt.degraded) return null;

  const spans = new Map<string, AbsoluteSpan>();
  for (const g of gantt.rows) {
    const rowId = resolveGanttKey(rows, g.key);
    if (rowId === null) continue;
    const start = gantt.originMs + g.startMs;
    const span: AbsoluteSpan = { start, end: start + (g.durationMs ?? 0) };
    const prev = spans.get(rowId);
    spans.set(
      rowId,
      prev === undefined ? span : { start: Math.min(prev.start, span.start), end: Math.max(prev.end, span.end) },
    );
  }

  for (const row of rows) {
    if (row.kind === "user" || spans.has(row.id) || row.serverMs === null) continue;
    spans.set(row.id, { start: row.serverMs, end: row.serverMs });
  }

  const userRow = rows.find((r) => r.kind === "user");
  if (userRow !== undefined) {
    const starts = Array.from(spans.values(), (s) => s.start);
    const at = starts.length > 0 ? Math.min(...starts) : fallbackStart;
    if (at !== null) spans.set(userRow.id, { start: at, end: at });
  }
  return spans;
}

/** 最后一条带合法 id 的帧的 `(serverMs, receivedAtMs)` —— 调用方拿它把客户端
 *  的「现在」校准到服务端时钟(客户端与服务端有时钟差,直接拿
 *  `Date.now()` 当轴末端会让运行中的尾块跳)。原样搬自当时的
 *  `lane_strip_model.ts`(该文件已随 PR-A.2 Task 11 退役),再往上源头是
 *  `components/turn/TurnCard.tsx` 的生长条锚点。 */
export function lastKnownFrame(
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
