import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { summarizeTurn } from "../turn_summary";

function updates(messages: unknown[]): SseEvent {
  return {
    id: "u",
    event: "updates",
    data: { agent: { messages } },
    rawData: "",
    receivedAt: "2026-06-29T00:00:00Z",
  };
}

describe("summarizeTurn", () => {
  it("sums usage across AI messages and splits cache/reasoning details", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "",
          usage_metadata: {
            input_tokens: 100,
            output_tokens: 20,
            total_tokens: 120,
            input_token_details: { cache_read: 64 },
            output_token_details: { reasoning: 8 },
          },
        },
      ]),
      updates([
        {
          type: "ai",
          content: "final",
          usage_metadata: { input_tokens: 50, output_tokens: 10, total_tokens: 60 },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finalText).toBe("final");
    // #8 — the empty first content is skipped; only real texts aggregate.
    expect(summary.assistantTexts).toEqual(["final"]);
    expect(summary.usage).toEqual({
      inputTokens: 150,
      outputTokens: 30,
      totalTokens: 180,
      cacheReadTokens: 64,
      cacheCreationTokens: 0,
      reasoningTokens: 8,
    });
  });

  it("collects reasoning_content blocks in order", () => {
    const events = [
      updates([
        { type: "ai", content: "answer", additional_kwargs: { reasoning_content: "step 1" } },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.reasoning).toEqual(["step 1"]);
    expect(summary.finalText).toBe("answer");
    expect(summary.assistantTexts).toEqual(["answer"]);
  });

  it("#8 — aggregates every AI text into assistantTexts (arrival order) while finalText stays last-wins", () => {
    const events = [
      updates([{ type: "ai", content: "step one text" }]),
      updates([{ type: "ai", content: "step two text" }]),
      updates([{ type: "ai", content: "the final text" }]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.assistantTexts).toEqual([
      "step one text",
      "step two text",
      "the final text",
    ]);
    expect(summary.finalText).toBe("the final text");
  });

  it("returns null usage when no AI message reports usage", () => {
    const events = [updates([{ type: "ai", content: "hi" }])];
    const summary = summarizeTurn(events);
    expect(summary.usage).toBeNull();
    expect(summary.finalText).toBe("hi");
    expect(summary.assistantTexts).toEqual(["hi"]);
  });

  it("takes the highest node step_count and the frame-span latency", () => {
    const events: SseEvent[] = [
      {
        id: "a",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "" }], step_count: 1 } },
        rawData: "",
        receivedAt: "2026-06-29T00:00:00.000Z",
      },
      {
        id: "b",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "done" }], step_count: 3 } },
        rawData: "",
        receivedAt: "2026-06-29T00:00:02.500Z",
      },
    ];
    const summary = summarizeTurn(events);
    expect(summary.stepCount).toBe(3);
    expect(summary.latencyMs).toBe(2500);
  });

  it("ignores non-updates frames and tool messages", () => {
    const events: SseEvent[] = [
      { id: "m", event: "metadata", data: { run_id: "r" }, rawData: "", receivedAt: "t" },
      updates([{ type: "tool", content: "tool out", tool_call_id: "c1", name: "x" }]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finalText).toBeNull();
    expect(summary.assistantTexts).toEqual([]);
    expect(summary.usage).toBeNull();
  });

  it("takes finish_reason and model_name from the last AI message that reports them", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "",
          response_metadata: { finish_reason: "tool_calls", model_name: "glm-5.2" },
        },
      ]),
      updates([
        {
          type: "ai",
          content: "done",
          response_metadata: { finish_reason: "stop", model_name: "glm-5.2" },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finishReason).toBe("stop");
    expect(summary.modelName).toBe("glm-5.2");
  });

  it("leaves finishReason/modelName null when response_metadata is absent", () => {
    const summary = summarizeTurn([updates([{ type: "ai", content: "hi" }])]);
    expect(summary.finishReason).toBeNull();
    expect(summary.modelName).toBeNull();
  });

  it("sums cache_creation into cacheCreationTokens", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "x",
          usage_metadata: {
            input_tokens: 10,
            output_tokens: 2,
            total_tokens: 12,
            input_token_details: { cache_read: 4, cache_creation: 6 },
          },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.usage?.cacheCreationTokens).toBe(6);
    expect(summary.usage?.cacheReadTokens).toBe(4);
  });

  it("emits per-step usage rows keyed by node + step_count without dropping the sum", () => {
    const events: SseEvent[] = [
      {
        id: "a", event: "updates",
        data: { agent: { step_count: 1, messages: [
          { type: "ai", content: "", usage_metadata: { input_tokens: 100, output_tokens: 10, total_tokens: 110 } },
        ] } },
        rawData: "", receivedAt: "2026-07-10T00:00:00Z",
      },
      {
        id: "b", event: "updates",
        data: { agent: { step_count: 2, messages: [
          { type: "ai", content: "done", usage_metadata: { input_tokens: 50, output_tokens: 5, total_tokens: 55 } },
        ] } },
        rawData: "", receivedAt: "2026-07-10T00:00:01Z",
      },
    ];
    const s = summarizeTurn(events);
    expect(s.perStepUsage).toHaveLength(2);
    expect(s.perStepUsage[0]).toEqual({
      node: "agent", stepCount: 1,
      usage: { inputTokens: 100, outputTokens: 10, totalTokens: 110, cacheReadTokens: 0, cacheCreationTokens: 0, reasoningTokens: 0 },
    });
    expect(s.perStepUsage[1].stepCount).toBe(2);
    // summed usage still intact
    expect(s.usage?.totalTokens).toBe(165);
  });
});
