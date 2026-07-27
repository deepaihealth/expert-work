/**
 * trace_banner tests — Task 10. Pure status derivation feeding the exact
 * view's RunStatusBanner.
 */
import { describe, expect, it } from "vitest";

import { traceBannerModel } from "../trace_banner";
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

describe("traceBannerModel", () => {
  it("ok trace with no error span → status ok + latency/cost, no error fields", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const llm = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "LLM 调用" });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 5900, totalCostUsd: 0.0021, spanCount: 2 },
      spans: [root, llm],
    };

    const result = traceBannerModel(trace);

    expect(result).toEqual({
      status: "ok",
      errorSpanId: null,
      errorLabel: null,
      errorMessage: null,
      latencyMs: 5900,
      totalCostUsd: 0.0021,
    });
  });

  it("trace with an error-level span → status error + errorLabel (label + detail) + errorMessage + errorSpanId", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const tool = makeSpan({
      id: "r1",
      parentId: "r0",
      kind: "tool",
      label: "工具调用",
      detail: "exec_python",
      level: "error",
      statusMessage: "SandboxTimeout: 执行超过 30s",
    });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 5900, totalCostUsd: null, spanCount: 2 },
      spans: [root, tool],
    };

    const result = traceBannerModel(trace);

    expect(result).toEqual({
      status: "error",
      errorSpanId: "r1",
      errorLabel: "工具调用 · exec_python",
      errorMessage: "SandboxTimeout: 执行超过 30s",
      latencyMs: 5900,
      totalCostUsd: null,
    });
  });

  it("error span with no detail → errorLabel is just the label", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const llm = makeSpan({
      id: "r1",
      parentId: "r0",
      kind: "llm",
      label: "LLM 调用",
      level: "error",
      statusMessage: "RateLimitError",
    });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 100, totalCostUsd: null, spanCount: 2 },
      spans: [root, llm],
    };

    expect(traceBannerModel(trace)?.errorLabel).toBe("LLM 调用");
  });

  it("non-ok status (not_ready) → null", () => {
    const trace: RunTrace = { status: "not_ready" };
    expect(traceBannerModel(trace)).toBeNull();
  });

  it("non-ok status (unavailable) → null", () => {
    const trace: RunTrace = { status: "unavailable" };
    expect(traceBannerModel(trace)).toBeNull();
  });

  it("ok status with no spans → null", () => {
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 0, totalCostUsd: null, spanCount: 0 },
      spans: [],
    };
    expect(traceBannerModel(trace)).toBeNull();
  });

  it("ok status with spans undefined → null", () => {
    const trace: RunTrace = { status: "ok" };
    expect(traceBannerModel(trace)).toBeNull();
  });
});
