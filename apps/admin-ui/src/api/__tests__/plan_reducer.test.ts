import { describe, expect, it } from "vitest";

import { foldPlan, planFromEvent, reducePlan } from "../plan_reducer";
import type { SseEvent } from "../sessions";

const PLAN_A = {
  goal: "给客户 C-1024 出续约建议",
  steps: [
    { id: "1", description: "查档案", status: "completed" },
    { id: "2", description: "分析工单", status: "in_progress" },
  ],
};
const PLAN_B = { goal: PLAN_A.goal, steps: [...PLAN_A.steps, { id: "3", description: "出建议", status: "pending" }] };

function evt(event: string, data: unknown, id: string | null = null): SseEvent {
  return { id, event, data, rawData: JSON.stringify(data), receivedAt: "" };
}

describe("planFromEvent", () => {
  it("reads a top-level plan frame (PR1) verbatim, keyed by its frame id", () => {
    expect(planFromEvent(evt("plan", PLAN_A, "1755500000000-7"), 3)).toEqual({
      plan: PLAN_A,
      sourceKey: "1755500000000-7",
    });
  });
  it("reads updates.<node>.plan (pre-PR1 persisted runs), last node wins", () => {
    const e = evt("updates", { tools: { plan: PLAN_A }, planner: { plan: PLAN_B } }, null);
    expect(planFromEvent(e, 5)).toEqual({ plan: PLAN_B, sourceKey: "5" });
  });
  it("ignores updates without a plan key / null plan / other events", () => {
    expect(planFromEvent(evt("updates", { agent: { messages: [] } }), 0)).toBeNull();
    expect(planFromEvent(evt("updates", { tools: { plan: null } }), 0)).toBeNull();
    expect(planFromEvent(evt("token", { step: 1, channel: "content", text: "x" }), 0)).toBeNull();
    expect(planFromEvent(evt("plan", "not-an-object"), 0)).toBeNull();
    expect(planFromEvent(evt("plan", { goal: "g" }), 0)).toBeNull(); // no steps array → not a plan
  });
});

describe("reducePlan", () => {
  it("returns the last snapshot in order (plan frame after updates.plan wins)", () => {
    const r = reducePlan([
      evt("updates", { tools: { plan: PLAN_A } }, "1"),
      evt("updates", { agent: { messages: [] } }, "2"),
      evt("plan", PLAN_B, "3"),
    ]);
    expect(r).toEqual({ plan: PLAN_B, sourceKey: "3" });
  });
  it("returns null for a stream with no plan", () => {
    expect(reducePlan([evt("metadata", { run_id: "r" }), evt("end", "ok")])).toBeNull();
  });
});

// I2 — the live event array is rebuilt (append-only) on every SSE frame, so a
// full rescan per frame is O(session). ``foldPlan`` resumes from the previous
// fold when the prefix is byte-for-byte the same objects, and must stay
// snapshot-identical to a full ``reducePlan`` — including the id-less
// `${index}` sourceKey fallback, which is an ABSOLUTE index.
describe("foldPlan", () => {
  // Deliberately id-less: sourceKey then falls back to the absolute index, so
  // an incremental fold that mis-numbered the tail would be caught.
  const noise = (n: number): SseEvent[] =>
    Array.from({ length: n }, () => evt("updates", { agent: { messages: [] } }));

  it("scans everything with no previous fold, and matches reducePlan", () => {
    const events = [...noise(3), evt("plan", PLAN_A), ...noise(2)];
    const fold = foldPlan(events, null);
    expect(fold.scanned).toBe(6);
    expect(fold.snapshot).toEqual({ plan: PLAN_A, sourceKey: "3" });
    expect(fold.snapshot).toEqual(reducePlan(events));
  });

  it("scans only the appended tail when the prefix objects are unchanged", () => {
    const events = [...noise(3), evt("plan", PLAN_A), ...noise(2)];
    const prev = foldPlan(events, null);

    const appended = [...events, evt("updates", { agent: { messages: [] } })];
    const carried = foldPlan(appended, prev);
    expect(carried.scanned).toBe(1);
    // The carried-over snapshot survives a tail with no plan in it.
    expect(carried.snapshot).toEqual(reducePlan(appended));

    // A plan frame in the tail lands with its ABSOLUTE index as sourceKey.
    const withPlan = [...appended, evt("plan", PLAN_B)];
    const next = foldPlan(withPlan, carried);
    expect(next.scanned).toBe(1);
    expect(next.snapshot).toEqual({ plan: PLAN_B, sourceKey: "7" });
    expect(next.snapshot).toEqual(reducePlan(withPlan));
  });

  it("re-scans in full when the prefix is not the same objects (rebuild) or the array shrank (reset)", () => {
    const events = [...noise(3), evt("plan", PLAN_A)];
    const prev = foldPlan(events, null);

    // Same content, different objects — e.g. a history rebuild.
    const rebuilt = events.map((e) => ({ ...e }));
    const reFold = foldPlan(rebuilt, prev);
    expect(reFold.scanned).toBe(4);
    expect(reFold.snapshot).toEqual(reducePlan(rebuilt));

    // Shorter array (run reset / thread switch) — nothing to carry over.
    const cleared = foldPlan([], prev);
    expect(cleared.scanned).toBe(0);
    expect(cleared.snapshot).toBeNull();
  });
});
