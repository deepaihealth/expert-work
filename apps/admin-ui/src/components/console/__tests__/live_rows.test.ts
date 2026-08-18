import { describe, expect, it } from "vitest";

import { liveSyntheticRows, settledStepsOf } from "../live_rows";
import type { SseEvent } from "../../../api/sessions";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";

/** A settled ``updates`` frame for the given agent step (parseTimeline turns
 *  this into an ``AgentStep`` item with a non-null ``stepCount``). */
function agentUpdate(step: number): SseEvent {
  return {
    id: null,
    event: "updates",
    rawData: "",
    receivedAt: "",
    data: { agent: { step_count: step, messages: [{ type: "ai", content: "done" }] } },
  };
}

function liveStep(overrides: Partial<LiveStep> = {}): LiveStep {
  return { content: "", reasoning: "", toolNames: new Map(), reasoningMs: null, ...overrides };
}

describe("settledStepsOf", () => {
  it("collects the stepCount of every settled agent step", () => {
    expect(settledStepsOf([agentUpdate(1), agentUpdate(2)])).toEqual(new Set([1, 2]));
  });
  it("returns an empty set for events with no settled agent step", () => {
    expect(settledStepsOf([])).toEqual(new Set());
  });
});

describe("liveSyntheticRows", () => {
  it("emits a think + tool row only for the unsettled step, ignoring the settled step's leftover buffer", () => {
    const events = [agentUpdate(1)]; // only step 1 has landed
    const liveByStep = new Map<number, LiveStep>([
      [1, liveStep({ content: "settled leftover", reasoning: "stale" })],
      [2, liveStep({ content: "partial", reasoning: "thinking…", toolNames: new Map([[0, "query_crm"]]) })],
    ]);
    const rows = liveSyntheticRows(events, liveByStep);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      id: "live-think:2", kind: "think", seq: -1, step: 2, status: "running",
      text: "thinking…", content: "partial", model: null, inputTokens: 0, outputTokens: 0,
      durationMs: null, eventIndexes: [], serverMs: null,
    });
    expect(rows[1]).toMatchObject({
      id: "live-tool:2:0", kind: "tool", seq: -1, step: 2, status: "running",
      durationMs: null, eventIndexes: [], serverMs: null,
      entry: { id: "live-2-0", rawName: "query_crm", toolName: "query_crm", isMcp: false, server: null, args: {}, status: "pending", resultPreview: null, durationMs: null },
    });
  });
  it("returns [] when liveByStep is undefined", () => {
    expect(liveSyntheticRows([], undefined)).toEqual([]);
  });
  it("orders unsettled steps ascending by step number, not Map insertion order", () => {
    const liveByStep = new Map<number, LiveStep>([
      [5, liveStep({ reasoning: "step five" })],
      [3, liveStep({ reasoning: "step three" })], // inserted after 5, must still sort before it
    ]);
    const rows = liveSyntheticRows([], liveByStep);
    expect(rows.map((r) => r.step)).toEqual([3, 5]);
  });
  it("skips a step with empty reasoning and no tool names (nothing to synthesize)", () => {
    const liveByStep = new Map<number, LiveStep>([[1, liveStep()]]);
    expect(liveSyntheticRows([], liveByStep)).toEqual([]);
  });
});
