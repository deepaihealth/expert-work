import { describe, expect, it } from "vitest";

import { computeSessionStats, type StatsTurnInput } from "../session_stats";
import type { SseEvent } from "../sessions";

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
});
