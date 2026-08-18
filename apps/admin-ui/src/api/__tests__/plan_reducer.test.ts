import { describe, expect, it } from "vitest";

import { planFromEvent, reducePlan } from "../plan_reducer";
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
