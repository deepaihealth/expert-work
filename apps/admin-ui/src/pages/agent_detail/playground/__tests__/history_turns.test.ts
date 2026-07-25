import { describe, expect, it } from "vitest";

import type { HistoryMessage } from "../../../../api/sessions";
import type { ThreadRunSummary } from "../../../../api/runs";
import { buildHistoryTurns } from "../history_turns";

function run(runId: string): ThreadRunSummary {
  return { runId, status: "success", isResume: false, createdAt: "2026-01-01" };
}

const U = (content: string): HistoryMessage => ({ role: "user", content });
const A = (content: string): HistoryMessage => ({ role: "assistant", content });

describe("buildHistoryTurns", () => {
  it("pairs each (user, following assistant) with the i-th run in order", () => {
    const turns = buildHistoryTurns(
      [U("q1"), A("a1"), U("q2"), A("a2")],
      [run("r1"), run("r2")],
    );
    expect(turns).toEqual([
      { key: "r1", input: "q1", fallbackAnswer: "a1", runId: "r1", status: "success" },
      { key: "r2", input: "q2", fallbackAnswer: "a2", runId: "r2", status: "success" },
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
      fallbackAnswer: "",
      runId: "r2",
      status: "success",
    });
  });

  it("returns [] for an empty thread", () => {
    expect(buildHistoryTurns([], [])).toEqual([]);
  });

  it("collects ALL assistant messages in a turn (a run can emit several), joined by blank lines", () => {
    // Turn 1: 3 assistant messages before the next user turn (e.g. multi-step run).
    // Turn 2: 1 assistant message. runs.length === 2 user turns → pairing still succeeds.
    const turns = buildHistoryTurns(
      [U("q1"), A("a1"), A("a2"), A("a3"), U("q2"), A("b1")],
      [run("r1"), run("r2")],
    );
    expect(turns).toEqual([
      { key: "r1", input: "q1", fallbackAnswer: "a1\n\na2\n\na3", runId: "r1", status: "success" },
      { key: "r2", input: "q2", fallbackAnswer: "b1", runId: "r2", status: "success" },
    ]);
  });
});
