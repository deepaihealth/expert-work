/**
 * Pre-first-token breakdown — 一期 Task 6。
 *
 * Folds a run's trace into the segments shown above the waterfall: each
 * top-level entry-chain span, then the first LLM call. Pure — no fetching.
 */
import type { TraceSpan } from "../../../api/trace_facade";

export interface BreakdownSegment {
  id: string;
  label: string;
  latencyMs: number;
}

export function buildBreakdown(spans: readonly TraceSpan[]): BreakdownSegment[] {
  const entry = spans.filter((s) => s.group === "entry");
  if (entry.length === 0) return [];
  // Children of an entry span also carry group="entry" (recall's embed /
  // retrieve / …). Counting them would make the segments sum past the
  // parent's own latency and the bar wider than the elapsed time — take
  // only the ones whose parent is outside this group.
  const entryIds = new Set(entry.map((s) => s.id));
  const topLevel = entry.filter((s) => s.parentId === null || !entryIds.has(s.parentId));

  const firstLlm = spans
    .filter((s) => s.kind === "llm")
    .sort((a, b) => a.startMs - b.startMs)[0];

  const segments = topLevel
    .slice()
    .sort((a, b) => a.startMs - b.startMs)
    .map((s) => ({ id: s.id, label: s.label, latencyMs: s.latencyMs }));

  if (firstLlm !== undefined) {
    segments.push({ id: firstLlm.id, label: firstLlm.label, latencyMs: firstLlm.latencyMs });
  }
  return segments;
}
