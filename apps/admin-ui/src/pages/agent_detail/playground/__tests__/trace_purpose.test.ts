/**
 * trace_purpose tests — Batch 4b Task 5, A' purpose labelling.
 *
 * See labelPurpose's doc comment for the 1:1-or-nothing rule this covers.
 */
import { describe, expect, it } from "vitest";

import { labelPurpose } from "../trace_purpose";
import type { RunTrace, TraceSpan } from "../../../../api/trace_facade";

function makeSpan(
  over: Partial<TraceSpan> & Pick<TraceSpan, "id" | "parentId" | "kind" | "label">,
): TraceSpan {
  return {
    detail: null,
    startMs: 0,
    latencyMs: 0,
    model: null,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    input: null,
    output: null,
    level: "default",
    statusMessage: null,
    ...over,
    purpose: over.purpose ?? "",
    group: over.group ?? null,
  };
}

const PRIMARY = "主推理";

describe("labelPurpose", () => {
  it("labels every llm span '主推理' when the llm span count matches agentStepCount 1:1", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const llm1 = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "LLM 调用", startMs: 100 });
    const tool = makeSpan({ id: "r2", parentId: "r0", kind: "tool", label: "工具调用", detail: "get_weather" });
    const llm2 = makeSpan({ id: "r3", parentId: "r0", kind: "llm", label: "LLM 调用", startMs: 300 });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 500, totalCostUsd: null, spanCount: 4 },
      spans: [root, llm1, tool, llm2],
    };

    const result = labelPurpose(trace, 2, PRIMARY);

    expect(result).not.toBe(trace);
    expect(result.spans?.find((s) => s.id === "r1")?.detail).toBe(PRIMARY);
    expect(result.spans?.find((s) => s.id === "r3")?.detail).toBe(PRIMARY);
    // Non-llm spans are untouched.
    expect(result.spans?.find((s) => s.id === "r0")?.detail).toBeNull();
    expect(result.spans?.find((s) => s.id === "r2")?.detail).toBe("get_weather");
    // The original trace/spans are never mutated.
    expect(trace.spans?.find((s) => s.id === "r1")?.detail).toBeNull();
  });

  it("excludes auxiliary llm spans (non-empty purpose) from the count and never labels them primary", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const main1 = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "LLM 调用", purpose: "main" });
    const aux = makeSpan({ id: "r2", parentId: "r0", kind: "llm", label: "记忆抽取", purpose: "memory" });
    const main2 = makeSpan({ id: "r3", parentId: "r0", kind: "llm", label: "LLM 调用", purpose: "main" });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 500, totalCostUsd: null, spanCount: 4 },
      spans: [root, main1, aux, main2],
    };

    // 2 main llm spans match agentStepCount 2 — the aux span does not inflate it.
    const result = labelPurpose(trace, 2, PRIMARY);

    expect(result.spans?.find((s) => s.id === "r1")?.detail).toBe(PRIMARY);
    expect(result.spans?.find((s) => s.id === "r3")?.detail).toBe(PRIMARY);
    // The auxiliary span is never labelled primary — no "记忆抽取 · 主推理".
    expect(result.spans?.find((s) => s.id === "r2")?.detail).toBeNull();
  });

  it("does not mislabel an aux span primary when a cache-hit turn (no llm span) balances the count", () => {
    // Regression for the collision: a cache-hit agent turn emits an agent frame
    // (agentStepCount 1) but no llm span, plus one memory-extraction aux span.
    // Counting all llm spans (1) would equal 1 and stamp 主推理 on the aux span;
    // counting only main spans (0 != 1) prevents it.
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const aux = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "记忆抽取", purpose: "memory" });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 500, totalCostUsd: null, spanCount: 2 },
      spans: [root, aux],
    };

    const result = labelPurpose(trace, 1, PRIMARY);

    expect(result).toBe(trace); // main count 0 != agentStepCount 1 → nothing labelled
    expect(result.spans?.find((s) => s.id === "r1")?.detail).toBeNull();
  });

  it("leaves spans untouched when the llm span count does not match agentStepCount (hidden sub-call, e.g. memory extraction)", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const llm1 = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "LLM 调用" });
    const llm2 = makeSpan({ id: "r2", parentId: "r0", kind: "llm", label: "LLM 调用" });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 500, totalCostUsd: null, spanCount: 3 },
      spans: [root, llm1, llm2],
    };

    const result = labelPurpose(trace, 1, PRIMARY);

    expect(result).toBe(trace);
    expect(result.spans?.every((s) => s.detail === null)).toBe(true);
  });

  it("returns the trace unchanged when status is not 'ok'", () => {
    const trace: RunTrace = { status: "not_ready" };

    expect(labelPurpose(trace, 1, PRIMARY)).toBe(trace);
  });

  it("returns the trace unchanged when spans is missing (malformed ok payload)", () => {
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 0, totalCostUsd: null, spanCount: 0 },
    };

    expect(labelPurpose(trace, 0, PRIMARY)).toBe(trace);
  });
});
