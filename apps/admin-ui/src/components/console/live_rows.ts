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
import type { CompactRow, ToolRow, TrajectoryRow } from "../../api/trajectory_rows";
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
  const rows: CompactRow[] = [];
  for (const step of unsettledSteps(events, liveByStep)) {
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
    for (const row of liveToolRows(step, live)) rows.push(row);
  }
  return rows;
}

/** 一个未落帧步里每个「已报名字、还没结果」的工具调用一条行 —— 中栏紧凑投影
 *  与账本投影共用(两处的工具行完全同形,只有 agent 步的投影不同)。 */
function liveToolRows(step: number, live: LiveStep): ToolRow[] {
  const rows: ToolRow[] = [];
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
  return rows;
}

/** 未落帧步号,升序 —— 两个 live 投影共用的取步逻辑。 */
function unsettledSteps(
  events: readonly SseEvent[],
  liveByStep: ReadonlyMap<number, LiveStep>,
): number[] {
  const settled = settledStepsOf(events);
  return Array.from(liveByStep.keys())
    .filter((step) => !settled.has(step))
    .sort((a, b) => a - b);
}

/** 账本用的 live 合成行(spec §九 D2)—— 每个未落帧的步**总是**一条
 *  `assistant` 行(哪怕思考与正文都还是空的:账本一步一行,空行由 UI 渲染成
 *  「(仅工具调用)」),后面跟着该步每个已命名工具的 `tool` 行。用量类字段一律
 *  0 / null(权威 `updates` 帧到达前后端还没报)。 */
export function liveLedgerRows(
  events: readonly SseEvent[],
  liveByStep: ReadonlyMap<number, LiveStep> | undefined,
): TrajectoryRow[] {
  if (!liveByStep) return [];
  const rows: TrajectoryRow[] = [];
  for (const step of unsettledSteps(events, liveByStep)) {
    const live = liveByStep.get(step);
    if (!live) continue;
    rows.push({
      id: `live-assistant:${step}`,
      kind: "assistant",
      seq: -1,
      step,
      status: "running",
      durationMs: null,
      eventIndexes: [],
      serverMs: null,
      text: live.content,
      reasoning: live.reasoning,
      model: null,
      inputTokens: 0,
      outputTokens: 0,
      finishReason: null,
      toolCallCount: live.toolNames.size,
    });
    for (const row of liveToolRows(step, live)) rows.push(row);
  }
  return rows;
}
