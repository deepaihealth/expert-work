import { describe, expect, it } from "vitest";
import { buildBreakdown } from "../entry_breakdown";
import type { TraceSpan } from "../../../../api/trace_facade";

const span = (o: Partial<TraceSpan>): TraceSpan => ({
  id: "x", parentId: null, kind: "span", label: "l", detail: null,
  startMs: 0, latencyMs: 0, model: null, inputTokens: null, outputTokens: null,
  costUsd: null, input: null, output: null, level: "default",
  statusMessage: null, purpose: "", group: null, ...o,
});

describe("buildBreakdown", () => {
  it("takes only top-level entry spans, not their children", () => {
    // 命门:recall 的子 span 也带 group="entry",全算进去的话总和会
    // 超过 recall 本身,分解条宽度加起来大于 100%。
    const spans = [
      span({ id: "r", label: "记忆召回", group: "entry", startMs: 0, latencyMs: 2000 }),
      span({ id: "e", parentId: "r", label: "向量化", group: "entry", startMs: 10, latencyMs: 200 }),
    ];
    const out = buildBreakdown(spans);
    expect(out.map((s) => s.label)).toEqual(["记忆召回"]);
  });

  it("ends the bar at the first llm span", () => {
    const spans = [
      span({ id: "r", label: "记忆召回", group: "entry", startMs: 0, latencyMs: 2000 }),
      span({ id: "l", kind: "llm", label: "LLM 调用", startMs: 2000, latencyMs: 600 }),
      span({ id: "l2", kind: "llm", label: "LLM 调用", startMs: 5000, latencyMs: 600 }),
    ];
    const out = buildBreakdown(spans);
    expect(out.at(-1)?.label).toBe("LLM 调用");
    expect(out).toHaveLength(2);
  });

  it("returns an empty breakdown when the trace has no entry spans", () => {
    expect(buildBreakdown([span({ kind: "llm", label: "LLM 调用" })])).toEqual([]);
  });
});
