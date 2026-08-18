/**
 * Live-turn synthetic compact rows — the still-streaming step(s) a live
 * turn's ``useTokenStream`` buffer holds that haven't landed as an
 * authoritative ``updates`` frame yet, projected into the same
 * ``CompactRow`` shape ``api/trajectory_rows.ts`` (Task 4) produces so
 * Task 11's TurnBlock and Task 18's TrajectoryPanel can render one list
 * instead of each splicing the live buffer in separately. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-5-brief.md.
 */
import { parseTimeline } from "../../api/timeline";
import type { SseEvent } from "../../api/sessions";
import type { CompactRow } from "../../api/trajectory_rows";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";

/** Agent step numbers that already have an authoritative ``updates`` frame —
 *  same algorithm as ``TurnCard.tsx:466-472``: every ``parseTimeline`` item
 *  whose ``kind === "agent"`` and ``stepCount`` is non-null. */
export function settledStepsOf(events: readonly SseEvent[]): Set<number> {
  const settled = new Set<number>();
  for (const item of parseTimeline(events)) {
    if (item.kind === "agent" && item.stepCount !== null) settled.add(item.stepCount);
  }
  return settled;
}

/** Synthetic compact rows for a live turn's still-streaming (not yet
 *  settled) steps, in ascending step order — one ``think`` row when the
 *  step's buffered reasoning is non-empty, plus one ``tool`` row per
 *  named-but-unresolved tool call. */
export function liveSyntheticRows(
  events: readonly SseEvent[],
  liveByStep: ReadonlyMap<number, LiveStep> | undefined,
): CompactRow[] {
  if (!liveByStep) return [];
  const settled = settledStepsOf(events);
  const steps = Array.from(liveByStep.keys())
    .filter((step) => !settled.has(step))
    .sort((a, b) => a - b);

  const rows: CompactRow[] = [];
  for (const step of steps) {
    const live = liveByStep.get(step);
    if (!live) continue;
    if (live.reasoning !== "") {
      rows.push({
        id: `live-think:${step}`,
        kind: "think",
        seq: -1,
        step,
        status: "running",
        durationMs: null,
        eventIndexes: [],
        serverMs: null,
        text: live.reasoning,
        content: live.content,
        model: null,
        inputTokens: 0,
        outputTokens: 0,
        finishReason: null,
      });
    }
    for (const [toolIdx, name] of live.toolNames) {
      rows.push({
        id: `live-tool:${step}:${toolIdx}`,
        kind: "tool",
        seq: -1,
        step,
        status: "running",
        durationMs: null,
        eventIndexes: [],
        serverMs: null,
        entry: {
          id: `live-${step}-${toolIdx}`,
          rawName: name,
          isMcp: false,
          server: null,
          toolName: name,
          args: {},
          status: "pending",
          resultPreview: null,
          durationMs: null,
        },
      });
    }
  }
  return rows;
}
