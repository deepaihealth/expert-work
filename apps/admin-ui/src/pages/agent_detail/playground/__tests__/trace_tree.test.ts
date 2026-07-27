import { describe, expect, it } from "vitest";

import type { TraceSpan } from "../../../../api/trace_facade";
import { buildRows, isWideBar } from "../trace_tree";

function span(id: string, parentId: string | null, startMs: number): TraceSpan {
  return {
    id,
    parentId,
    kind: "span",
    label: id,
    detail: null,
    startMs,
    latencyMs: 1,
    model: null,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    input: null,
    output: null,
    level: "default",
    statusMessage: null,
    purpose: "",
    group: null,
  };
}

describe("buildRows", () => {
  it("preorders parents before children and sorts siblings by startMs", () => {
    // root → [b(startMs 5), a(startMs 1)] ; a → a1(startMs 2)
    const rows = buildRows([
      span("root", null, 0),
      span("b", "root", 5),
      span("a", "root", 1),
      span("a1", "a", 2),
    ]);
    expect(rows.map((r) => r.span.id)).toEqual(["root", "a", "a1", "b"]);
  });

  it("assigns depth by nesting level across >2 levels", () => {
    const rows = buildRows([
      span("r", null, 0),
      span("l1", "r", 0),
      span("l2", "l1", 0),
      span("l3", "l2", 0),
    ]);
    expect(rows.map((r) => [r.span.id, r.depth])).toEqual([
      ["r", 0],
      ["l1", 1],
      ["l2", 2],
      ["l3", 3],
    ]);
  });

  it("tracks per-ancestor `continues` so guides know which ancestors have more siblings below", () => {
    // r → [a, b(last)] ; a → [a1(last)]
    const rows = buildRows([
      span("r", null, 0),
      span("a", "r", 0),
      span("b", "r", 1),
      span("a1", "a", 0),
    ]);
    // `continues[i]` reflects whether the ANCESTOR at depth i has more siblings
    // below it (→ draw a through-line in that column), not the node itself.
    const byId = Object.fromEntries(rows.map((r) => [r.span.id, r.continues]));
    expect(byId["a"]).toEqual([false]); // r is the only root → its column never continues
    expect(byId["a1"]).toEqual([false, true]); // r no sibs; a HAS sibling b below → a's column continues
    expect(byId["b"]).toEqual([false]); // r no sibs
  });

  it("handles multiple roots (parentId null) in startMs order", () => {
    const rows = buildRows([span("r2", null, 9), span("r1", null, 1)]);
    expect(rows.map((r) => r.span.id)).toEqual(["r1", "r2"]);
  });
});

describe("isWideBar", () => {
  it("is wide at/above 50% of the timeline, narrow below", () => {
    expect(isWideBar(50)).toBe(true);
    expect(isWideBar(100)).toBe(true);
    expect(isWideBar(49.9)).toBe(false);
    expect(isWideBar(0)).toBe(false);
  });
});
