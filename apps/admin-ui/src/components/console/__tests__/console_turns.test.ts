import { describe, expect, it } from "vitest";

import { buildConsoleTurns, statsInputOf } from "../console_turns";

const meta = (runId: string) => ({ id: "1", event: "metadata", data: { run_id: runId }, rawData: "", receivedAt: "" });

describe("buildConsoleTurns", () => {
  it("orders history before live, numbers seq from 0, maps history status/error like the old TurnCard call site", () => {
    const out = buildConsoleTurns({
      historyTurns: [
        { key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null, createdAt: null },
        { key: "h2", input: "q2", fallbackLines: [{ text: "partial", channel: "final" }], runId: "r2", status: "timeout", tokens: null, createdAt: null },
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
    expect(buildConsoleTurns({ historyTurns: [{ key: "h", input: "", fallbackLines: [], runId: "r", status: "success", tokens, createdAt: null }], historyLoads: {}, liveTurns: [], timings: {} })[0].tokens).toEqual(tokens);
    expect(buildConsoleTurns({ historyTurns: null, historyLoads: {}, liveTurns: [], timings: {} })).toEqual([]);
  });
});

describe("createdAt", () => {
  it("history turns carry the run's createdAt, live turns null", () => {
    const [history, live] = buildConsoleTurns({
      historyTurns: [{ key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null, createdAt: "2026-01-01T00:00:00Z" }],
      historyLoads: {},
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [meta("r3")], status: "running", error: null, approval: null }],
      timings: {},
    });
    expect(history.createdAt).toBe("2026-01-01T00:00:00Z");
    expect(live.createdAt).toBeNull();
  });
});

describe("statsInputOf", () => {
  it("maps loaded from loadState==='done' (not source) and carries events/status/tokens/timing through", () => {
    const [history, live] = buildConsoleTurns({
      historyTurns: [{ key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null, createdAt: "2026-01-01T00:00:00Z" }],
      historyLoads: {}, // no r1 entry → loadState stays "pending", not "done"
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [meta("r3")], status: "running", error: null, approval: null }],
      timings: { L1: { ttftMs: 500, firstTokenAt: 1, lastTokenAt: 2 } },
    });
    expect(statsInputOf(history)).toMatchObject({ loaded: false, status: "done", events: [], tokens: null, timing: null });
    expect(statsInputOf(live)).toMatchObject({ loaded: true, status: "running", events: [meta("r3")], tokens: null, timing: { ttftMs: 500 } });
  });
});

// ---------------------------------------------------------------------------
// D-5/D-6 — non-terminal history turns (live tail) + in-place approval
// ---------------------------------------------------------------------------

const approvalFrame = {
  id: "9",
  event: "approval",
  data: {
    run_id: "r-paused",
    thread_id: "th-1",
    request_id: "approval:abc",
    reason_kind: "missing_info",
    action_summary: "which quarter?",
    requested_at: "2026-08-23T00:00:00Z",
    timeout_at: "2026-08-23T01:00:00Z",
  },
  rawData: "",
  receivedAt: "",
};

describe("non-terminal history turns (D-5/D-6)", () => {
  it("maps a running run to a running turn and hook-internal 'live' load to 'done'", () => {
    const out = buildConsoleTurns({
      historyTurns: [
        { key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null, createdAt: null },
        { key: "h2", input: "q2", fallbackLines: [], runId: "r2", status: "running", tokens: null, createdAt: null },
      ],
      historyLoads: {
        r1: { state: "done", events: [meta("r1")] },
        r2: { state: "live", events: [meta("r2")] },
      },
      liveTurns: [],
      timings: {},
    });
    expect(out[1]).toMatchObject({
      loadState: "done",
      turn: { status: "running", error: null },
    });
    expect(out[1].turn.events).toHaveLength(1);
  });

  it("surfaces the pending approval on a paused LAST turn only", () => {
    const paused = { key: "h2", input: "q2", fallbackLines: [], runId: "r-paused", status: "paused", tokens: null, createdAt: null };
    const withApproval = buildConsoleTurns({
      historyTurns: [
        { key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null, createdAt: null },
        paused,
      ],
      historyLoads: { "r-paused": { state: "live", events: [meta("r-paused"), approvalFrame] } },
      liveTurns: [],
      timings: {},
    });
    expect(withApproval[1].turn.status).toBe("running");
    expect(withApproval[1].turn.approval).toMatchObject({
      run_id: "r-paused",
      thread_id: "th-1",
      reason_kind: "missing_info",
      action_summary: "which quarter?",
    });

    // Same paused run followed by its continuation → its approval was
    // decided; showing live buttons would 409. No approval synthesised.
    const withContinuation = buildConsoleTurns({
      historyTurns: [
        paused,
        { key: "h3", input: "", fallbackLines: [], runId: "r3", status: "running", tokens: null, createdAt: null },
      ],
      historyLoads: { "r-paused": { state: "live", events: [approvalFrame] } },
      liveTurns: [],
      timings: {},
    });
    expect(withContinuation[0].turn.approval).toBeNull();
  });

  it("does not surface approval on a paused turn when a live turn follows", () => {
    const out = buildConsoleTurns({
      historyTurns: [
        { key: "h2", input: "q2", fallbackLines: [], runId: "r-paused", status: "paused", tokens: null, createdAt: null },
      ],
      historyLoads: { "r-paused": { state: "done", events: [approvalFrame] } },
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [], status: "running", error: null, approval: null }],
      timings: {},
    });
    expect(out[0].turn.approval).toBeNull();
  });
});
