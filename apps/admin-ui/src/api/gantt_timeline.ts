/**
 * Gantt data-layer assembler — flattens a turn's parsed timeline (agent/aux
 * steps, tool calls, worker delegations, markers) into positioned rows for
 * the Gantt execution-trace view.
 *
 * Absolute time comes ONLY from the SSE frame `id`'s millisecond segment
 * (`serverMsOf`) — never `receivedAt`, which for replayed turns is bunched
 * at the replay instant rather than the original wall-clock time. Frames
 * are queued after the action they describe completes, so a frame's id ms
 * is (approximately) that unit's *end* time; `start = end - durationMs`.
 * See docs/superpowers/specs/2026-07-31-gantt-execution-timeline-design.md.
 *
 * A missing/malformed frame id degrades that row to sequential placement
 * right after the previous row's end (never crashes, never drops the row)
 * and flips `GanttModel.degraded`. A still-pending tool call (no RESULT
 * frame yet) is not a degrade — it has no id yet because it hasn't
 * finished, so it is placed the same way as an in-progress agent/aux row.
 */
import type { SseEvent } from "./sessions";
import { parseTimeline, type AuxNodeItem, type MarkerItem, type TimelineItem } from "./timeline";
import type { WorkerStepSummary, WorkerTimeline } from "./worker_timeline";

// M2 — `serverMsOf` now lives in the dependency-free `sse_id.ts` leaf
// module; re-exported here so existing `import { serverMsOf } from
// "./gantt_timeline"` call sites (and tests) keep working unchanged.
export { serverMsOf } from "./sse_id";

export interface GanttRow {
  key: string;
  label: string;
  model?: string;
  kind: "agent" | "aux" | "tool" | "worker" | "final";
  depth: 0 | 1 | 2;
  /** Relative to `t0` (the earliest row start), milliseconds. */
  startMs: number;
  /** `null` = in progress (growing bar) or duration unknown. */
  durationMs: number | null;
  /** I1 — agent rows take `AgentStep.hasError` (a dispatched tool call
   *  failed); tool rows take `status === "error"`; worker/aux rows have no
   *  per-unit error signal upstream yet, so they default `false`. Drives
   *  the Gantt row's `data-error` anchor (jump-to-error) and its bar's
   *  danger tint. */
  hasError: boolean;
  detail:
    | { type: "item"; item: TimelineItem } // agent/aux 行 → 整卡复用
    | { type: "parentStep"; item: TimelineItem }; // tool/worker 行 → 所属步整卡
}

export interface GanttMarker {
  atMs: number;
  kind: MarkerItem["kind"];
  text: string;
}

export interface GanttModel {
  rows: GanttRow[];
  markers: GanttMarker[];
  /** Axis length, `max(end) - t0`. */
  totalMs: number;
  /** `true` if any row's frame id was missing/malformed and had to fall
   *  back to sequential placement. */
  degraded: boolean;
}

function isAuxItem(item: TimelineItem): item is AuxNodeItem {
  return (
    item.kind === "memory_recall" ||
    item.kind === "planner" ||
    item.kind === "reflect" ||
    item.kind === "memory_writeback" ||
    item.kind === "workspace_ingest"
  );
}

interface Placed {
  startMs: number;
  endMs: number;
  durationMs: number | null;
}

/** Position one unit on the absolute-ms axis. `serverMs` null (missing or
 *  malformed frame id) degrades the row to right after `prevEnd`. A valid
 *  `serverMs` with `durationMs` null is an in-progress/duration-less unit,
 *  anchored at `prevEnd`; its end is the frame's own (last known) moment —
 *  not a data anomaly, so it never flips `degraded`. */
function place(
  serverMs: number | null | undefined,
  durationMs: number | null,
  prevEnd: number,
): { placed: Placed; degraded: boolean } {
  if (serverMs === null || serverMs === undefined) {
    const start = prevEnd;
    return { placed: { startMs: start, endMs: start + (durationMs ?? 0), durationMs }, degraded: true };
  }
  const start = durationMs === null ? prevEnd : serverMs - durationMs;
  return { placed: { startMs: start, endMs: serverMs, durationMs }, degraded: false };
}

/** Flatten a worker's (and its nested children's) step frames into a flat
 *  list — Gantt caps at depth 2, so grandchild delegations render at the
 *  same depth as their parent's steps rather than indenting further. */
function collectWorkerSteps(
  workers: readonly WorkerTimeline[],
): Array<{ worker: WorkerTimeline; step: WorkerStepSummary }> {
  const out: Array<{ worker: WorkerTimeline; step: WorkerStepSummary }> = [];
  for (const w of workers) {
    for (const step of w.steps) out.push({ worker: w, step });
    if (w.children.length > 0) out.push(...collectWorkerSteps(w.children));
  }
  return out;
}

/** I3① — the absolute ms of the first row (in the same traversal order the
 *  main loop below uses: agent → its tools → their workers, then aux/marker
 *  items) that carries a valid frame-id ms. Seeds `buildGanttRows`' initial
 *  `prevEnd` so a degraded row (missing/malformed id) that precedes every
 *  valid-id row anchors near the real data instead of absolute-ms `0`
 *  (~1970) — which previously collapsed the axis to a decades-wide span
 *  (`totalMs` in the trillions, `ticksFor` looping toward a gridline per
 *  30s across that whole span). `null` when no row anywhere in the turn has
 *  a valid id — callers then keep the pre-existing behaviour (chain starts
 *  at 0), which is harmless there since the whole axis is relative anyway. */
function firstValidAnchorMs(items: readonly TimelineItem[]): number | null {
  for (const item of items) {
    if (item.kind === "agent") {
      if (item.serverMs != null) return item.serverMs;
      for (const tool of item.tools) {
        const pending = tool.status === "pending" || tool.status === "pending_approval";
        if (!pending && tool.serverMs != null) return tool.serverMs;
        for (const { step } of collectWorkerSteps(tool.workers ?? [])) {
          if (step.serverMs != null) return step.serverMs;
        }
      }
      continue;
    }
    if (item.serverMs != null) return item.serverMs;
  }
  return null;
}

interface AbsRow {
  key: string;
  label: string;
  model?: string;
  kind: GanttRow["kind"];
  depth: 0 | 1 | 2;
  absStart: number;
  absEnd: number;
  durationMs: number | null;
  hasError: boolean;
  detail: GanttRow["detail"];
}

export function buildGanttRows(
  events: readonly SseEvent[],
  opts?: { settled?: boolean },
): GanttModel {
  const items = parseTimeline(events);
  const absRows: AbsRow[] = [];
  const absMarkers: GanttMarker[] = [];
  // I3① — see `firstValidAnchorMs` doc.
  let prevEnd = firstValidAnchorMs(items) ?? 0;
  let degraded = false;

  for (const item of items) {
    if (item.kind === "agent") {
      const { placed, degraded: stepDegraded } = place(item.serverMs, item.durationMs, prevEnd);
      prevEnd = placed.endMs;
      degraded = degraded || stepDegraded;
      absRows.push({
        key: `item-${item.seq}`,
        label: `步骤 ${item.stepCount ?? item.seq + 1}`,
        model: item.model ?? undefined,
        kind: "agent",
        depth: 0,
        absStart: placed.startMs,
        absEnd: placed.endMs,
        durationMs: placed.durationMs,
        hasError: item.hasError,
        detail: { type: "item", item },
      });

      for (const tool of item.tools) {
        // A still-pending call (no RESULT yet) has no serverMs because it
        // hasn't finished — that's an in-progress row, not a degrade.
        const pending = tool.status === "pending" || tool.status === "pending_approval";
        const { placed: toolPlaced, degraded: toolDegraded } = pending
          ? { placed: { startMs: prevEnd, endMs: prevEnd, durationMs: null }, degraded: false }
          : place(tool.serverMs, tool.durationMs, prevEnd);
        prevEnd = toolPlaced.endMs;
        degraded = degraded || toolDegraded;
        absRows.push({
          key: `tool-${tool.id}`,
          label: tool.toolName,
          kind: "tool",
          depth: 1,
          absStart: toolPlaced.startMs,
          absEnd: toolPlaced.endMs,
          durationMs: toolPlaced.durationMs,
          hasError: tool.status === "error",
          detail: { type: "parentStep", item },
        });

        for (const { worker, step } of collectWorkerSteps(tool.workers ?? [])) {
          const { placed: stepPlaced, degraded: stepPlacedDegraded } = place(
            step.serverMs,
            step.durationMs,
            prevEnd,
          );
          prevEnd = stepPlaced.endMs;
          degraded = degraded || stepPlacedDegraded;
          absRows.push({
            key: `worker-${worker.workerId}-${step.wseq}`,
            label: `${worker.label} · ${step.node}`,
            kind: "worker",
            depth: 2,
            absStart: stepPlaced.startMs,
            absEnd: stepPlaced.endMs,
            durationMs: stepPlaced.durationMs,
            // I1 — no per-step error signal upstream yet (WorkerStepSummary
            // carries no status/error field); default false rather than
            // guess from the parent worker's terminal status.
            hasError: false,
            detail: { type: "parentStep", item },
          });
        }
      }
      continue;
    }

    if (isAuxItem(item)) {
      const { placed, degraded: auxDegraded } = place(item.serverMs, item.durationMs, prevEnd);
      prevEnd = placed.endMs;
      degraded = degraded || auxDegraded;
      absRows.push({
        key: `item-${item.seq}`,
        label: item.summary,
        kind: "aux",
        depth: 0,
        absStart: placed.startMs,
        absEnd: placed.endMs,
        durationMs: placed.durationMs,
        // I1 — aux nodes (memory recall / planner / reflect / writeback)
        // have no error concept, only `tone: "normal" | "warn"`.
        hasError: false,
        detail: { type: "item", item },
      });
      continue;
    }

    // Remaining kinds are markers — instantaneous, never a row.
    const { placed: markerPlaced } = place(item.serverMs, 0, prevEnd);
    prevEnd = markerPlaced.endMs;
    absMarkers.push({ atMs: markerPlaced.startMs, kind: item.kind, text: item.text });
  }

  const t0 = absRows.length > 0 ? Math.min(...absRows.map((r) => r.absStart)) : 0;
  const totalMs = absRows.length > 0 ? Math.max(...absRows.map((r) => r.absEnd)) - t0 : 0;

  const rows: GanttRow[] = absRows.map((r) => ({
    key: r.key,
    label: r.label,
    model: r.model,
    kind: r.kind,
    depth: r.depth,
    startMs: r.absStart - t0,
    durationMs: r.durationMs,
    hasError: r.hasError,
    detail: r.detail,
  }));

  // settle 后末步即终结步(与 #1072 final 语义对齐:api/turn_summary.ts
  // 只有"末条且不带 tool_calls"才是 final)— relabel the last agent-kind
  // row only when it carries no tool calls; a settled run whose last step
  // dispatched tools (guard/error/max_steps 中断在工具调用后) stays "agent",
  // not a false "终结步".
  if (opts?.settled === true) {
    for (let i = rows.length - 1; i >= 0; i -= 1) {
      if (rows[i].kind === "agent") {
        const d = rows[i].detail;
        const noToolCalls = d.type === "item" && d.item.kind === "agent" && d.item.tools.length === 0;
        if (noToolCalls) rows[i] = { ...rows[i], kind: "final" };
        break;
      }
    }
  }

  const markers: GanttMarker[] = absMarkers.map((m) => ({ atMs: m.atMs - t0, kind: m.kind, text: m.text }));

  return { rows, markers, totalMs, degraded };
}
