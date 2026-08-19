import { describe, expect, it } from "vitest";
import type { RunTrace, TraceSpan } from "../trace_facade";
import { matchTraceSpans } from "../trace_match";
import type { TrajectoryRow } from "../trajectory_rows";
import type { WorkerTimeline } from "../worker_timeline";

// `eventIndexes` needs its own cast — plain `as const` would narrow the
// empty literal to `readonly []`, which isn't assignable to `RowBase`'s
// mutable `number[]`.
const base = { seq: 0, step: null, status: "ok", durationMs: null, eventIndexes: [] as number[], serverMs: null } as const;
function span(p: Partial<TraceSpan> & Pick<TraceSpan, "id" | "kind" | "label">): TraceSpan {
  return { parentId: null, detail: null, startMs: 0, latencyMs: 10, model: null, inputTokens: null, outputTokens: null, costUsd: null,
    input: null, output: null, level: "default", statusMessage: null, purpose: "", group: null, ...p };
}
const okTrace = (spans: TraceSpan[]): RunTrace => ({ status: "ok", trace: { name: "run", latencyMs: 100, totalCostUsd: 0.01, spanCount: spans.length }, spans });
const think = (id: string, seq: number): TrajectoryRow => ({ ...base, id, seq, kind: "think", text: "", content: null, model: null, inputTokens: 0, outputTokens: 0, finishReason: null });
const tool = (id: string, name: string): TrajectoryRow => ({ ...base, id, kind: "tool", entry: { id, rawName: name, isMcp: false, server: null, toolName: name, args: {}, status: "success", resultPreview: null, durationMs: 1 } });
const assistant = (id: string, seq: number, step: number | null): TrajectoryRow => ({ ...base, id, seq, step, kind: "assistant", text: "", reasoning: "", model: null, inputTokens: 0, outputTokens: 0, finishReason: null, toolCallCount: 0 });

describe("matchTraceSpans", () => {
  it("no trace / not ok → every row no_trace", () => {
    const rows = [think("think:0", 0)];
    expect(matchTraceSpans(rows, null).get("think:0")).toEqual({ span: null, reason: "no_trace" });
    expect(matchTraceSpans(rows, { status: "not_ready" }).get("think:0")).toEqual({ span: null, reason: "no_trace" });
  });
  it("think rows pair with main llm spans in order only when counts match", () => {
    const rows = [think("think:0", 0), think("think:2", 2)];
    const l1 = span({ id: "l1", kind: "llm", label: "llm" }), l2 = span({ id: "l2", kind: "llm", label: "llm", purpose: "main" });
    const aux = span({ id: "a", kind: "llm", label: "llm", purpose: "memory" });
    const m = matchTraceSpans(rows, okTrace([aux, l1, l2]));
    expect(m.get("think:0")?.span?.id).toBe("l1");
    expect(m.get("think:2")?.span?.id).toBe("l2");
    const bad = matchTraceSpans(rows, okTrace([l1]));
    expect(bad.get("think:0")).toEqual({ span: null, reason: "count_mismatch" });
  });
  it("tool rows pair by label, nth-of-name; extra rows are count_mismatch", () => {
    const rows = [tool("tool:0:0", "query_crm"), tool("tool:0:1", "query_crm"), tool("tool:1:0", "send_mail")];
    const m = matchTraceSpans(rows, okTrace([span({ id: "t1", kind: "tool", label: "query_crm" }), span({ id: "t2", kind: "tool", label: "query_crm" })]));
    expect(m.get("tool:0:0")?.span?.id).toBe("t1");
    expect(m.get("tool:0:1")?.span?.id).toBe("t2");
    expect(m.get("tool:1:0")).toEqual({ span: null, reason: "count_mismatch" });
  });
  it("aux rows pair by purpose; user/assistant/markers are unsupported", () => {
    const rows: TrajectoryRow[] = [
      { ...base, id: "user", seq: -1, kind: "user", text: "q", attachmentNames: [], inputs: {} },
      { ...base, id: "memory:1", seq: 1, kind: "memory", direction: "writeback", count: 1, detail: {} },
      { ...base, id: "plan:2", seq: 2, kind: "plan", source: "planner", callId: null, plannerSeq: null, stepsTotal: 1, goal: null, reason: null, plan: null },
      { ...base, id: "error:3", seq: 3, kind: "error", text: "x" },
    ];
    const m = matchTraceSpans(rows, okTrace([span({ id: "p", kind: "llm", label: "llm", purpose: "planner" }), span({ id: "mm", kind: "llm", label: "llm", purpose: "memory" })]));
    expect(m.get("user")).toEqual({ span: null, reason: "unsupported" });
    expect(m.get("memory:1")?.span?.id).toBe("mm");
    expect(m.get("plan:2")?.span?.id).toBe("p");
    expect(m.get("error:3")).toEqual({ span: null, reason: "unsupported" });
  });

  // The verbatim task-14 brief tests above only exercise 2 of R15 rule 4's 5
  // purpose mappings and one MarkerRow kind out of rule 5's list — these
  // close the rest of the R15 matrix (self-review checklist).
  it("aux purpose table covers recall→rerank / reflect / compaction→compress, and a short purpose is count_mismatch", () => {
    const rows: TrajectoryRow[] = [
      { ...base, id: "memory:recall:0", seq: 0, kind: "memory", direction: "recall", count: 2, detail: {} },
      { ...base, id: "reflect:1", seq: 1, kind: "reflect", verdict: "pass", detail: {} },
      { ...base, id: "compaction:2", seq: 2, kind: "compaction", text: "compacted" },
      { ...base, id: "memory:recall:3", seq: 3, kind: "memory", direction: "recall", count: 1, detail: {} },
    ];
    const m = matchTraceSpans(
      rows,
      okTrace([
        span({ id: "rr", kind: "llm", label: "llm", purpose: "rerank" }),
        span({ id: "rf", kind: "llm", label: "llm", purpose: "reflect" }),
        span({ id: "cp", kind: "llm", label: "llm", purpose: "compress" }),
      ]),
    );
    expect(m.get("memory:recall:0")?.span?.id).toBe("rr");
    expect(m.get("reflect:1")?.span?.id).toBe("rf");
    expect(m.get("compaction:2")?.span?.id).toBe("cp");
    // Second "recall" row exceeds the single "rerank" span available.
    expect(m.get("memory:recall:3")).toEqual({ span: null, reason: "count_mismatch" });
  });

  it("update_plan plan rows pair against tool-kind spans labelled update_plan, alongside ordinary tool rows", () => {
    const rows: TrajectoryRow[] = [
      { ...base, id: "plan:0", seq: 0, kind: "plan", source: "update_plan", callId: "call-1", plannerSeq: null, stepsTotal: 2, goal: null, reason: null, plan: null },
      tool("tool:1:0", "query_crm"),
    ];
    const m = matchTraceSpans(
      rows,
      okTrace([
        span({ id: "up", kind: "tool", label: "update_plan" }),
        span({ id: "qc", kind: "tool", label: "query_crm" }),
      ]),
    );
    expect(m.get("plan:0")?.span?.id).toBe("up");
    expect(m.get("tool:1:0")?.span?.id).toBe("qc");
  });

  it("assistant / subagent / retry / approval / guard / gap rows are all unsupported", () => {
    const rows: TrajectoryRow[] = [
      assistant("assistant", -1, null),
      { ...base, id: "sub:0", seq: 0, kind: "subagent", worker: {} as unknown as WorkerTimeline, parentEntryId: "tool:0:0" },
      { ...base, id: "retry:1", seq: 1, kind: "retry", text: "retry" },
      { ...base, id: "approval:2", seq: 2, kind: "approval", text: "approval" },
      { ...base, id: "guard:3", seq: 3, kind: "guard", text: "guard" },
      { ...base, id: "gap:4", seq: 4, kind: "gap", text: "gap" },
    ];
    const m = matchTraceSpans(rows, okTrace([]));
    for (const row of rows) {
      expect(m.get(row.id)).toEqual({ span: null, reason: "unsupported" });
    }
  });
  it("per-step assistant rows pair with main llm spans in order; unequal counts mark them all count_mismatch", () => {
    const rows = [assistant("assistant:0", 0, 1), assistant("assistant:2", 2, 2)];
    const l1 = span({ id: "l1", kind: "llm", label: "llm" });
    const l2 = span({ id: "l2", kind: "llm", label: "llm", purpose: "main" });
    const aux = span({ id: "a", kind: "llm", label: "llm", purpose: "memory" });
    const m = matchTraceSpans(rows, okTrace([aux, l1, l2]));
    expect(m.get("assistant:0")?.span?.id).toBe("l1");
    expect(m.get("assistant:2")?.span?.id).toBe("l2");
    expect(matchTraceSpans(rows, okTrace([l1])).get("assistant:0")).toEqual({ span: null, reason: "count_mismatch" });
    // 旧投影末尾那条合成 assistant(step 为 null)不进 rule 2,仍旧 unsupported。
    expect(matchTraceSpans([assistant("assistant", -1, null)], okTrace([l1])).get("assistant"))
      .toEqual({ span: null, reason: "unsupported" });
  });
  it("Rule 2 pairs step rows with main llm spans in startMs order, not payload order (PR-A.3 §十.4)", () => {
    const rows = [assistant("assistant:1", 1, 1), assistant("assistant:3", 3, 2)];
    const late = span({ id: "llm-late", kind: "llm", label: "llm", purpose: "main", startMs: 32903, latencyMs: 7265 });
    const early = span({ id: "llm-early", kind: "llm", label: "llm", purpose: "main", startMs: 960, latencyMs: 29786 });
    const m = matchTraceSpans(rows, okTrace([late, early]));
    expect(m.get("assistant:1")?.span?.id).toBe("llm-early");
    expect(m.get("assistant:3")?.span?.id).toBe("llm-late");
  });
});
