/**
 * matchTraceSpans — pairs each trajectory row (Task 4) with its Langfuse
 * span, so the Timing tab's right column can render an exact-view deep
 * link per row. Pure; no fetching, no state (see ``useRunTrace`` for the
 * fetch/poll side).
 *
 * Matching rules (R15):
 *
 * 1. No usable trace (``null`` / not ``"ok"`` / no ``spans``) → every row
 *    is ``no_trace``.
 * 2. Per-step model rows — ``think`` rows (中栏紧凑投影) **or**
 *    ``assistant`` rows whose ``step`` is non-null (账本按步投影, spec §九
 *    D2) — ↔ main-conversation llm spans (``kind === "llm" && (purpose ===
 *    "" || purpose === "main")``), paired in order of appearance — same
 *    criterion as ``labelPurpose`` — but only when the counts are equal;
 *    otherwise every such row is ``count_mismatch``. 旧投影末尾那条整轮合成
 *    的 ``assistant`` 行 ``step`` 为 ``null``,不参与,落到 rule 5。
 * 3. ``tool`` rows and ``update_plan`` plan rows ↔ ``kind === "tool"``
 *    spans, matched by name (tool name / ``"update_plan"``), nth
 *    occurrence to nth occurrence; a name with fewer spans than rows
 *    leaves the extra rows ``count_mismatch``.
 * 4. Aux rows (memory recall/writeback, planner, reflect, compaction) ↔
 *    ``kind === "llm"`` spans sharing the same ``purpose``, same
 *    nth-of-purpose pairing as rule 3.
 * 5. Everything else (user / step-less assistant / subagent / retry /
 *    error / approval / guard / gap) is ``unsupported`` — there is no
 *    Langfuse span shape to pair it with.
 */
import type { RunTrace, TraceSpan } from "./trace_facade";
import type { TrajectoryRow } from "./trajectory_rows";

export type SpanMatchReason = "matched" | "count_mismatch" | "no_trace" | "unsupported";

export interface SpanMatch {
  span: TraceSpan | null;
  reason: SpanMatchReason;
}

function noTraceMap(rows: readonly TrajectoryRow[]): Map<string, SpanMatch> {
  const result = new Map<string, SpanMatch>();
  for (const row of rows) result.set(row.id, { span: null, reason: "no_trace" });
  return result;
}

/** nth-occurrence-of-key pairing shared by rules 3 and 4: rows and spans are
 *  each grouped by `key`, then matched in within-group appearance order.
 *  A row whose group index has no matching span is `count_mismatch`. */
function pairByKey(
  result: Map<string, SpanMatch>,
  rows: readonly TrajectoryRow[],
  keyOfRow: (row: TrajectoryRow) => string,
  spans: readonly TraceSpan[],
  keyOfSpan: (span: TraceSpan) => string,
): void {
  const spansByKey = new Map<string, TraceSpan[]>();
  for (const span of spans) {
    const key = keyOfSpan(span);
    const bucket = spansByKey.get(key);
    if (bucket) bucket.push(span);
    else spansByKey.set(key, [span]);
  }
  const seenCount = new Map<string, number>();
  for (const row of rows) {
    const key = keyOfRow(row);
    const index = seenCount.get(key) ?? 0;
    seenCount.set(key, index + 1);
    const span = spansByKey.get(key)?.[index] ?? null;
    result.set(row.id, span ? { span, reason: "matched" } : { span: null, reason: "count_mismatch" });
  }
}

/** Rule 4's purpose key for an aux row — `null` when the row isn't an aux
 *  kind (falls through to rule 5's `unsupported`). */
function auxPurposeOf(row: TrajectoryRow): string | null {
  if (row.kind === "memory") return row.direction === "recall" ? "rerank" : "memory";
  if (row.kind === "plan" && row.source === "planner") return "planner";
  if (row.kind === "reflect") return "reflect";
  if (row.kind === "compaction") return "compress";
  return null;
}

export function matchTraceSpans(
  rows: readonly TrajectoryRow[],
  trace: RunTrace | null,
): ReadonlyMap<string, SpanMatch> {
  if (trace === null || trace.status !== "ok" || !trace.spans) return noTraceMap(rows);
  const spans = trace.spans;
  const result = new Map<string, SpanMatch>();

  // Rule 2 — per-step model rows ↔ main llm spans, order-paired, count-gated.
  const stepRows = rows.filter(
    (row) => row.kind === "think" || (row.kind === "assistant" && row.step !== null),
  );
  const mainLlmSpans = spans
    .filter((span) => span.kind === "llm" && (span.purpose === "" || span.purpose === "main"))
    // 后端现在按 startMs 排了(PR-A.3),但配对的正确性不该依赖上游顺序。
    .slice()
    .sort((a, b) => a.startMs - b.startMs);
  if (stepRows.length === mainLlmSpans.length) {
    stepRows.forEach((row, i) => result.set(row.id, { span: mainLlmSpans[i], reason: "matched" }));
  } else {
    stepRows.forEach((row) => result.set(row.id, { span: null, reason: "count_mismatch" }));
  }

  // Rule 3 — tool rows + update_plan plan rows ↔ tool spans, nth-of-name.
  const toolLikeRows = rows.filter(
    (row) => row.kind === "tool" || (row.kind === "plan" && row.source === "update_plan"),
  );
  const toolSpans = spans.filter((span) => span.kind === "tool");
  pairByKey(
    result,
    toolLikeRows,
    (row) => (row.kind === "tool" ? row.entry.toolName : "update_plan"),
    toolSpans,
    (span) => span.label,
  );

  // Rule 4 — aux rows ↔ llm spans, nth-of-purpose.
  const auxRows = rows.filter((row) => auxPurposeOf(row) !== null);
  const auxLlmSpans = spans.filter((span) => span.kind === "llm");
  pairByKey(result, auxRows, (row) => auxPurposeOf(row) as string, auxLlmSpans, (span) => span.purpose);

  // Rule 5 — everything left unmatched has no Langfuse span shape.
  for (const row of rows) {
    if (!result.has(row.id)) result.set(row.id, { span: null, reason: "unsupported" });
  }

  return result;
}
