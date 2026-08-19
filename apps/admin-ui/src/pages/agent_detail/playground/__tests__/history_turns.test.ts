import { describe, expect, it } from "vitest";

import type { HistoryMessage } from "../../../../api/sessions";
import type { ThreadRunSummary } from "../../../../api/runs";
import { buildHistoryTurns } from "../history_turns";

function run(runId: string): ThreadRunSummary {
  return { runId, status: "success", isResume: false, createdAt: "2026-01-01", tokens: null };
}

const U = (content: string): HistoryMessage => ({ role: "user", content });
const A = (
  content: string,
  channel: HistoryMessage["channel"] = null,
): HistoryMessage => ({ role: "assistant", content, channel });

describe("buildHistoryTurns", () => {
  it("pairs each (user, following assistant) with the i-th run in order", () => {
    const turns = buildHistoryTurns(
      [U("q1"), A("a1"), U("q2"), A("a2")],
      [run("r1"), run("r2")],
    );
    expect(turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [{ text: "a1", channel: null }],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
      {
        key: "r2",
        input: "q2",
        fallbackLines: [{ text: "a2", channel: null }],
        runId: "r2",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
    ]);
  });

  it("carries each assistant row's channel through into its fallback line", () => {
    const turns = buildHistoryTurns(
      [U("q1"), A("thinking out loud", "commentary"), A("the answer", "final")],
      [run("r1")],
    );
    expect(turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [
          { text: "thinking out loud", channel: "commentary" },
          { text: "the answer", channel: "final" },
        ],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
    ]);
  });

  it("returns null when user-turn count != run count (approval split / stray runs)", () => {
    // 2 user turns, 3 runs (an approval split one turn into 2 runs) → degrade.
    expect(
      buildHistoryTurns([U("q1"), A("a1"), U("q2"), A("a2")], [run("r1"), run("r2"), run("r3")]),
    ).toBeNull();
  });

  it("tolerates a trailing user turn with no assistant reply (empty fallback)", () => {
    const turns = buildHistoryTurns([U("q1"), A("a1"), U("q2")], [run("r1"), run("r2")]);
    expect(turns?.[1]).toEqual({
      key: "r2",
      input: "q2",
      fallbackLines: [],
      runId: "r2",
      status: "success",
      tokens: null,
      createdAt: "2026-01-01",
    });
  });

  it("returns [] for an empty thread", () => {
    expect(buildHistoryTurns([], [])).toEqual([]);
  });

  it("collects ALL assistant messages in a turn (a run can emit several), each its own line", () => {
    // Turn 1: 3 assistant messages before the next user turn (e.g. multi-step run).
    // Turn 2: 1 assistant message. runs.length === 2 user turns → pairing still succeeds.
    const turns = buildHistoryTurns(
      [U("q1"), A("a1"), A("a2"), A("a3"), U("q2"), A("b1")],
      [run("r1"), run("r2")],
    );
    expect(turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [
          { text: "a1", channel: null },
          { text: "a2", channel: null },
          { text: "a3", channel: null },
        ],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
      {
        key: "r2",
        input: "q2",
        fallbackLines: [{ text: "b1", channel: null }],
        runId: "r2",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
    ]);
  });

  it("carries each run's persisted token rollup onto the paired turn", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "q", channel: null },
      { role: "assistant", content: "a", channel: "final" },
    ];
    const tokens = {
      input_tokens: 10, output_tokens: 5, cache_creation_tokens: 0,
      cache_read_tokens: 0, total_tokens: 15, llm_calls: 1, models: ["m"],
    };
    const turns = buildHistoryTurns(messages, [
      { runId: "r1", status: "success", isResume: false, createdAt: "2026-01-01T00:00:00Z", tokens },
    ]);
    expect(turns?.[0]?.tokens).toEqual(tokens);
  });
  it("carries each run's createdAt onto the paired turn (null when the summary lacks it)", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "q", channel: null },
      { role: "assistant", content: "a", channel: "final" },
    ];
    const turns = buildHistoryTurns(messages, [
      { runId: "r1", status: "success", isResume: false, createdAt: "2026-01-01T00:00:00Z", tokens: null },
    ]);
    expect(turns?.[0]?.createdAt).toBe("2026-01-01T00:00:00Z");
    const bare = buildHistoryTurns(messages, [
      { runId: "r1", status: "success", isResume: false, tokens: null } as unknown as Parameters<typeof buildHistoryTurns>[1][number],
    ]);
    expect(bare?.[0]?.createdAt).toBeNull();
  });
});
