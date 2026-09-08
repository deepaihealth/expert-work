import { describe, expect, it, vi } from "vitest";

import { rateKey, type RateBook } from "../cost";
import { attributedModelsOf, computeSessionStats, type StatsTurnInput } from "../session_stats";
import type { SseEvent } from "../sessions";
import * as turnSummarySdk from "../turn_summary";

function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return { id: null, event: "updates", data: { [node]: channels }, rawData: "", receivedAt: "" };
}
function aiStep(step: number, durationMs: number, usage: { in: number; out: number; cacheRead?: number }): SseEvent {
  return upd("agent", {
    step_count: step, _duration_ms: durationMs,
    messages: [{ type: "ai", content: "a", usage_metadata: {
      input_tokens: usage.in, output_tokens: usage.out, total_tokens: usage.in + usage.out,
      input_token_details: { cache_read: usage.cacheRead ?? 0 },
    } }],
  });
}
const toolsStep = (durationMs: number): SseEvent => upd("tools", { _duration_ms: durationMs, messages: [] });
const live = (events: SseEvent[], extra: Partial<StatsTurnInput> = {}): StatsTurnInput =>
  ({ events, loaded: true, status: "done", tokens: null, timing: null, ...extra });

describe("computeSessionStats", () => {
  it("sums turns/steps/LLM ms/tool ms/tokens across loaded turns; cache hit = cache_read ÷ input", () => {
    const s = computeSessionStats([
      live([aiStep(1, 800, { in: 1000, out: 100, cacheRead: 900 }), toolsStep(300), aiStep(2, 700, { in: 1200, out: 50, cacheRead: 1100 })]),
      live([aiStep(1, 500, { in: 500, out: 20 })]),
    ], null);
    expect(s).toMatchObject({ turns: 2, steps: 3, llmMs: 2000, toolMs: 300, inputTokens: 2700, outputTokens: 170, partial: false, costCny: null });
    expect(s.cacheHitPct).toBe(74); // 2000/2700
  });
  it("averages ttft and computes ≈tok/s from first/last token wall-clock", () => {
    const s = computeSessionStats([
      live([aiStep(1, 100, { in: 10, out: 300 })], { timing: { ttftMs: 800, firstTokenAt: 10_000, lastTokenAt: 12_000 } }),
      live([aiStep(1, 100, { in: 10, out: 100 })], { timing: { ttftMs: 400, firstTokenAt: 20_000, lastTokenAt: 21_000 } }),
      live([aiStep(1, 100, { in: 10, out: 999 })], { timing: null }), // no timing → excluded from tok/s
    ], null);
    expect(s.ttftAvgMs).toBe(600);
    expect(s.tokPerSec).toBe(133.3); // (300+100)/(2+1)s
  });
  it("uses the persisted rollup for unloaded history turns and flags partial when a turn has neither", () => {
    const s = computeSessionStats([
      { events: [], loaded: false, status: "done", timing: null, tokens: { input_tokens: 400, output_tokens: 40, cache_creation_tokens: 0, cache_read_tokens: 200, total_tokens: 440, llm_calls: 1, models: [] } },
      { events: [], loaded: false, status: "done", timing: null, tokens: null },
    ], null);
    expect(s).toMatchObject({ turns: 2, steps: 0, inputTokens: 400, outputTokens: 40, cacheHitPct: 50, partial: true });
  });
  it("prices with the rate card exactly like TurnCard.costCny (non-cached input + cache read + output)", () => {
    const rate = { input_per_mtok_micros: 3_000_000, cache_read_per_mtok_micros: 300_000, output_per_mtok_micros: 15_000_000 } as never;
    const s = computeSessionStats([live([aiStep(1, 1, { in: 1_000_000, out: 100_000, cacheRead: 400_000 })])], rate);
    // (600k*3e6 + 400k*3e5 + 100k*1.5e7)/1e12 = 1.8 + 0.12 + 1.5
    expect(s.costCny).toBeCloseTo(3.42, 6);
  });
  it("counts a running turn with no step yet; empty input → zeros/nulls", () => {
    expect(computeSessionStats([live([], { status: "running" })], null).turns).toBe(1);
    expect(computeSessionStats([], null)).toEqual({ turns: 0, steps: 0, llmMs: 0, toolMs: 0, ttftAvgMs: null, tokPerSec: null, cacheHitPct: null, inputTokens: 0, outputTokens: 0, costCny: null, partial: false });
  });
  it("excludes a loaded, settled turn with zero steps from the turns count (not merely 'count everything')", () => {
    // loaded && stepCount 0 && status "done" satisfies none of the three
    // turns disjuncts — a mutant that counted every turn would still pass
    // every other case above (they all happen to be counted).
    expect(computeSessionStats([live([], { status: "done" })], null).turns).toBe(0);
  });
  it("rounds cacheHitPct (not floors/truncates)", () => {
    // 2/3 * 100 = 66.666...  round → 67, floor/trunc → 66.
    const s = computeSessionStats([live([aiStep(1, 1, { in: 3, out: 1, cacheRead: 2 })])], null);
    expect(s.cacheHitPct).toBe(67);
  });
  it("clamps costCny's non-cached-input term at 0 when cache_read exceeds input (TurnCard.tsx's Math.max(0, …))", () => {
    const rate = { input_per_mtok_micros: 3_000_000, cache_read_per_mtok_micros: 300_000, output_per_mtok_micros: 15_000_000 } as never;
    // input=100, cacheRead=900 (unrealistic but exercises the clamp): an
    // unclamped (input − cacheRead) would go negative and under-price.
    const s = computeSessionStats([live([aiStep(1, 1, { in: 100, out: 0, cacheRead: 900 })])], rate);
    // (max(0,100-900)*3e6 + 900*3e5 + 0)/1e12 = (0 + 2.7e8)/1e12
    expect(s.costCny).toBeCloseTo(0.00027, 10);
  });

  // I2 — PlaygroundTab recomputes this over the WHOLE session on every SSE
  // frame (``consoleTurns`` is a fresh array each frame), so a settled turn
  // must not be re-summarised every time. The cache is keyed on the events
  // array identity: a live turn gets a brand-new array per frame (see
  // ``useRunEngine`` `events: [...tn.events, frame]`) and correctly misses.
  it("re-uses a turn's contribution when its events array identity is unchanged, and recomputes on a new array", () => {
    const spy = vi.spyOn(turnSummarySdk, "summarizeTurn");
    try {
      const settledEvents = [aiStep(1, 800, { in: 1000, out: 100, cacheRead: 900 }), toolsStep(300)];

      const first = computeSessionStats([live(settledEvents)], null);
      expect(spy).toHaveBeenCalledTimes(1);

      // Same array reference (a settled turn across frames) → cache hit.
      const second = computeSessionStats([live(settledEvents)], null);
      expect(spy).toHaveBeenCalledTimes(1);
      expect(second).toEqual(first);

      // A structurally-equal but freshly-allocated array (the streaming turn
      // every frame) must miss — the cache must not key on content.
      const third = computeSessionStats([live([...settledEvents])], null);
      expect(spy).toHaveBeenCalledTimes(2);
      expect(third).toEqual(first);
    } finally {
      spy.mockRestore();
    }
  });
});


it("counts a loaded interrupted turn with zero completed steps (终审 F6 — no 轮数 flicker on lazy load)", () => {
  expect(
    computeSessionStats([live([], { status: "interrupted" })], null).turns,
  ).toBe(1);
});


// ---------------------------------------------------------------------------
// B-42 — 会话级成本按 (provider, model) 分桶计价
// ---------------------------------------------------------------------------

function workerEndFrame(
  usage: { in: number; out: number; cacheRead?: number },
  bucket: { provider: string; model: string } | null,
): SseEvent {
  const um = {
    input_tokens: usage.in,
    output_tokens: usage.out,
    total_tokens: usage.in + usage.out,
    input_token_details: { cache_read: usage.cacheRead ?? 0, cache_creation: 0 },
    output_token_details: { reasoning: 0 },
  };
  const data: Record<string, unknown> = {
    outcome: "success",
    iteration_used: 1,
    llm_call_count: 1,
    wall_clock_ms: 10,
    usage: um,
  };
  if (bucket !== null) data.usage_by_model = [{ ...bucket, ...um }];
  return {
    id: null,
    event: "worker",
    data: { worker_id: "w-1", parent_worker_id: null, depth: 1, kind: "end", wseq: 2, data },
    rawData: "",
    receivedAt: "",
  };
}

const AGENT_CARD = {
  id: "rc-a",
  tenant_id: null,
  provider: "anthropic",
  model: "claude-x",
  input_per_mtok_micros: 3_000_000,
  output_per_mtok_micros: 15_000_000,
  cache_creation_per_mtok_micros: 0,
  cache_read_per_mtok_micros: 300_000,
};
const WORKER_CARD = {
  id: "rc-w",
  tenant_id: null,
  provider: "zhipu",
  model: "glm-5.3",
  input_per_mtok_micros: 500_000,
  output_per_mtok_micros: 2_000_000,
  cache_creation_per_mtok_micros: 0,
  cache_read_per_mtok_micros: 50_000,
};
const BOOK: RateBook = {
  agentKey: rateKey("anthropic", "claude-x"),
  byModel: new Map([
    [rateKey("anthropic", "claude-x"), AGENT_CARD],
    [rateKey("zhipu", "glm-5.3"), WORKER_CARD],
  ]),
};
const AGENT_ONLY: RateBook = {
  agentKey: rateKey("anthropic", "claude-x"),
  byModel: new Map([[rateKey("anthropic", "claude-x"), AGENT_CARD]]),
};

describe("computeSessionStats — cost by (provider, model)", () => {
  it("prices a loaded turn's worker bucket at the worker's own card", () => {
    const s = computeSessionStats(
      [live([aiStep(1, 1, { in: 1_000_000, out: 100_000, cacheRead: 400_000 }), workerEndFrame({ in: 2_000_000, out: 50_000 }, { provider: "zhipu", model: "glm-5.3" })])],
      BOOK,
    );
    // 主线 (600000×3 + 400000×0.3 + 100000×15)/1e6 = 3.42;worker (2000000×0.5 + 50000×2)/1e6 = 1.1
    expect(s.costCny).toBeCloseTo(4.52, 6);
    expect(s.inputTokens).toBe(3_000_000);
  });

  it("matches the old single-card total exactly when the worker shares the agent's model", () => {
    const s = computeSessionStats(
      [live([aiStep(1, 1, { in: 1_000_000, out: 100_000, cacheRead: 400_000 }), workerEndFrame({ in: 2_000_000, out: 50_000 }, { provider: "anthropic", model: "claude-x" })])],
      AGENT_ONLY,
    );
    const legacy = (Math.max(0, 3_000_000 - 400_000) * 3_000_000 + 400_000 * 300_000 + 150_000 * 15_000_000) / 1e12;
    expect(s.costCny).toBe(legacy);
  });

  it("falls back to the agent's card for a worker frame without usage_by_model (today's algorithm)", () => {
    const s = computeSessionStats(
      [live([aiStep(1, 1, { in: 1_000_000, out: 100_000, cacheRead: 400_000 }), workerEndFrame({ in: 2_000_000, out: 50_000 }, null)])],
      BOOK,
    );
    const legacy = (Math.max(0, 3_000_000 - 400_000) * 3_000_000 + 400_000 * 300_000 + 150_000 * 15_000_000) / 1e12;
    expect(s.costCny).toBe(legacy);
  });

  it("prices an unloaded history turn's persisted usage_by_model per model, and its bare rollup at the agent's card", () => {
    const rollup = { input_tokens: 3_000_000, output_tokens: 150_000, cache_creation_tokens: 0, cache_read_tokens: 400_000, total_tokens: 3_150_000, llm_calls: 2, models: ["claude-x", "glm-5.3"] };
    const split = computeSessionStats(
      [{ events: [], loaded: false, status: "done", timing: null, tokens: { ...rollup, usage_by_model: [
        { provider: "anthropic", model: "claude-x", input_tokens: 1_000_000, output_tokens: 100_000, cache_creation_tokens: 0, cache_read_tokens: 400_000, total_tokens: 1_100_000, llm_calls: 1 },
        { provider: "zhipu", model: "glm-5.3", input_tokens: 2_000_000, output_tokens: 50_000, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2_050_000, llm_calls: 1 },
      ] } }],
      BOOK,
    );
    expect(split.costCny).toBeCloseTo(4.52, 6);

    const bare = computeSessionStats(
      [{ events: [], loaded: false, status: "done", timing: null, tokens: rollup }],
      BOOK,
    );
    const legacy = (Math.max(0, 3_000_000 - 400_000) * 3_000_000 + 400_000 * 300_000 + 150_000 * 15_000_000) / 1e12;
    expect(bare.costCny).toBe(legacy);
  });

  it("hides the cost while an attributed model's card is still missing from the book", () => {
    const s = computeSessionStats(
      [live([aiStep(1, 1, { in: 10, out: 1 }), workerEndFrame({ in: 100, out: 10 }, { provider: "zhipu", model: "glm-5.3" })])],
      AGENT_ONLY,
    );
    expect(s.costCny).toBeNull();
  });
});

describe("attributedModelsOf", () => {
  it("lists every distinct (provider, model) the session's usage names — loaded frames and persisted rollups alike", () => {
    const turns: StatsTurnInput[] = [
      live([aiStep(1, 1, { in: 1, out: 1 }), workerEndFrame({ in: 1, out: 1 }, { provider: "zhipu", model: "glm-5.3" })]),
      live([workerEndFrame({ in: 1, out: 1 }, { provider: "zhipu", model: "glm-5.3" })]),
      { events: [], loaded: false, status: "done", timing: null, tokens: { input_tokens: 1, output_tokens: 1, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2, llm_calls: 1, models: ["kimi-k3"], usage_by_model: [
        { provider: "moonshot", model: "kimi-k3", input_tokens: 1, output_tokens: 1, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2, llm_calls: 1 },
        { provider: null, model: "legacy", input_tokens: 1, output_tokens: 1, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2, llm_calls: 1 },
      ] } },
    ];
    expect(attributedModelsOf(turns)).toEqual([
      { provider: "zhipu", model: "glm-5.3" },
      { provider: "moonshot", model: "kimi-k3" },
    ]);
  });
});
