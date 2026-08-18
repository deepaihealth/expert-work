/**
 * LaneStrip data-layer model — projects `buildGanttRows` output (`api/gantt_
 * timeline.ts`) into three lanes (input / model / tools) for the debug
 * console's compact time strip. Pure; no rendering, no state. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-15-brief.md.
 */
import type { SseEvent } from "../../api/sessions";
import {
  buildGanttRows,
  type GanttMarker,
  type GanttRow,
} from "../../api/gantt_timeline";
import { serverMsOf } from "../../api/sse_id";
import { resolveGanttKey, type TrajectoryRow } from "../../api/trajectory_rows";

export type Lane = "input" | "model" | "tools";

export interface LaneBlock {
  key: string;
  lane: Lane;
  rowId: string | null;
  label: string;
  startMs: number;
  durationMs: number | null;
  hasError: boolean;
}

export interface LaneMarker {
  key: string;
  atMs: number;
  kind: GanttMarker["kind"];
  text: string;
}

export interface LaneModel {
  blocks: LaneBlock[];
  markers: LaneMarker[];
  totalMs: number;
  degraded: boolean;
}

export const LANE_OF: Record<GanttRow["kind"], Lane> = {
  aux: "input",
  agent: "model",
  final: "model",
  tool: "tools",
  worker: "tools",
};

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

/** `buildGanttRows(events, { settled: !running })` → lane model. While
 *  `running`, `totalMs` grows to "now" using `nowMs` calibrated against the
 *  last frame carrying a valid id (`TurnCard.tsx:413-421`'s algorithm) —
 *  static (settled `totalMs`) when not running, or when no frame has a
 *  parseable id. */
export function laneModelOf(
  events: readonly SseEvent[],
  rows: readonly TrajectoryRow[],
  opts: { running: boolean; nowMs: number },
): LaneModel {
  const gantt = buildGanttRows(events, { settled: !opts.running });

  const blocks: LaneBlock[] = gantt.rows.map((row) => ({
    key: row.key,
    lane: LANE_OF[row.kind],
    rowId: resolveGanttKey(rows, row.key),
    label: row.label,
    startMs: row.startMs,
    durationMs: row.durationMs,
    hasError: row.hasError,
  }));

  const markers: LaneMarker[] = gantt.markers.map((marker, i) => ({
    key: `${marker.kind}-${marker.atMs}-${i}`,
    atMs: marker.atMs,
    kind: marker.kind,
    text: marker.text,
  }));

  let totalMs = gantt.totalMs;
  if (opts.running) {
    const last = lastKnownFrame(events);
    if (last !== null) {
      const nowServerMs = last.serverMs + (opts.nowMs - last.receivedAtMs);
      totalMs = gantt.totalMs + Math.max(0, nowServerMs - last.serverMs);
    }
  }

  return { blocks, markers, totalMs, degraded: gantt.degraded };
}
