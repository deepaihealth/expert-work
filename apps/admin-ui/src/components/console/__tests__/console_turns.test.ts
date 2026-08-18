import { describe, expect, it } from "vitest";

import { buildConsoleTurns, statsInputOf } from "../console_turns";

const meta = (runId: string) => ({ id: "1", event: "metadata", data: { run_id: runId }, rawData: "", receivedAt: "" });

describe("buildConsoleTurns", () => {
  it("orders history before live, numbers seq from 0, maps history status/error like the old TurnCard call site", () => {
    const out = buildConsoleTurns({
      historyTurns: [
        { key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null },
        { key: "h2", input: "q2", fallbackLines: [{ text: "partial", channel: "final" }], runId: "r2", status: "timeout", tokens: null },
      ],
      historyLoads: { r1: { state: "done", events: [meta("r1")] } },
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [meta("r3")], status: "running", error: null, approval: null }],
      timings: { L1: { ttftMs: 500, firstTokenAt: 1, lastTokenAt: 2 } },
    });
    expect(out.map((t) => [t.key, t.seq, t.source])).toEqual([["h1", 0, "history"], ["h2", 1, "history"], ["L1", 2, "live"]]);
    expect(out[0]).toMatchObject({ runId: "r1", loadState: "done", turn: { status: "done", error: null } });
    expect(out[1]).toMatchObject({ runId: "r2", loadState: "pending", turn: { status: "error", error: "timeout", events: [] }, fallbackLines: [{ text: "partial", channel: "final" }] });
    expect(out[2]).toMatchObject({ runId: "r3", loadState: "done", timing: { ttftMs: 500 }, tokens: null });
  });
  it("passes the persisted rollup through and returns [] for null history + no live turns", () => {
    const tokens = { input_tokens: 1, output_tokens: 1, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2, llm_calls: 1, models: [] };
    expect(buildConsoleTurns({ historyTurns: [{ key: "h", input: "", fallbackLines: [], runId: "r", status: "success", tokens }], historyLoads: {}, liveTurns: [], timings: {} })[0].tokens).toEqual(tokens);
    expect(buildConsoleTurns({ historyTurns: null, historyLoads: {}, liveTurns: [], timings: {} })).toEqual([]);
  });
});

describe("statsInputOf", () => {
  it("maps loaded from loadState==='done' (not source) and carries events/status/tokens/timing through", () => {
    const [history, live] = buildConsoleTurns({
      historyTurns: [{ key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null }],
      historyLoads: {}, // no r1 entry → loadState stays "pending", not "done"
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [meta("r3")], status: "running", error: null, approval: null }],
      timings: { L1: { ttftMs: 500, firstTokenAt: 1, lastTokenAt: 2 } },
    });
    expect(statsInputOf(history)).toMatchObject({ loaded: false, status: "done", events: [], tokens: null, timing: null });
    expect(statsInputOf(live)).toMatchObject({ loaded: true, status: "running", events: [meta("r3")], tokens: null, timing: { ttftMs: 500 } });
  });
});
