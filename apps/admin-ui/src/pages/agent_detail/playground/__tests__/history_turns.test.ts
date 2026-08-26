import { describe, expect, it } from "vitest";

import type { HistoryMessage } from "../../../../api/sessions";
import type { ThreadRunSummary } from "../../../../api/runs";
import { buildHistoryTurns } from "../history_turns";

function run(
  runId: string,
  status: ThreadRunSummary["status"] = "success",
): ThreadRunSummary {
  return { runId, status, isResume: false, createdAt: "2026-01-01", finishedAt: null, error: null, tokens: null };
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
      { runId: "r1", status: "success", isResume: false, createdAt: "2026-01-01T00:00:00Z", finishedAt: null, error: null, tokens },
    ]);
    expect(turns?.[0]?.tokens).toEqual(tokens);
  });
  it("carries each run's createdAt onto the paired turn (null when the summary lacks it)", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "q", channel: null },
      { role: "assistant", content: "a", channel: "final" },
    ];
    const turns = buildHistoryTurns(messages, [
      { runId: "r1", status: "success", isResume: false, createdAt: "2026-01-01T00:00:00Z", finishedAt: null, error: null, tokens: null },
    ]);
    expect(turns?.[0]?.createdAt).toBe("2026-01-01T00:00:00Z");
    const bare = buildHistoryTurns(messages, [
      { runId: "r1", status: "success", isResume: false, tokens: null } as unknown as Parameters<typeof buildHistoryTurns>[1][number],
    ]);
    expect(bare?.[0]?.createdAt).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// D-5 — trailing non-terminal tolerance
// ---------------------------------------------------------------------------

describe("buildHistoryTurns non-terminal tail", () => {
  it("builds a turn for a trailing running run whose user message is checkpointed", () => {
    const built = buildHistoryTurns(
      [U("q1"), A("a1"), U("q2")],
      [run("r1"), run("r2", "running")],
    );
    expect(built).not.toBeNull();
    expect(built).toHaveLength(2);
    expect(built![1]).toMatchObject({ runId: "r2", status: "running", input: "q2" });
  });

  it("builds a turn for a trailing running run whose user message is NOT yet checkpointed", () => {
    const built = buildHistoryTurns([U("q1"), A("a1")], [run("r1"), run("r2", "running")]);
    expect(built).not.toBeNull();
    expect(built).toHaveLength(2);
    expect(built![1]).toMatchObject({ runId: "r2", status: "running", input: "" });
    expect(built![1].fallbackLines).toEqual([]);
  });

  it("tolerates paused + just-spawned continuation as one trailing block", () => {
    // 2 user turns, 3 runs: r2 paused (owns q2), r3 = its continuation (no
    // user message of its own, still running).
    const built = buildHistoryTurns(
      [U("q1"), A("a1"), U("q2")],
      [run("r1"), run("r2", "paused"), run("r3", "running")],
    );
    expect(built).not.toBeNull();
    expect(built).toHaveLength(3);
    expect(built![1]).toMatchObject({ runId: "r2", status: "paused", input: "q2" });
    expect(built![2]).toMatchObject({ runId: "r3", status: "running", input: "" });
  });

  it("still degrades when a non-terminal run sits BEFORE the trailing block", () => {
    // paused mid-thread (its continuation already finished) — order-pairing
    // is ambiguous, keep the honest flat fallback (ROADMAP D-7).
    expect(
      buildHistoryTurns(
        [U("q1"), A("a1"), U("q2"), A("a2")],
        [run("r1"), run("r2", "paused"), run("r3")],
      ),
    ).toBeNull();
  });

  it("still degrades when even the tolerant window cannot explain the counts", () => {
    // 3 user turns but only 1 terminal + 1 running run — more pairs than runs.
    expect(
      buildHistoryTurns(
        [U("q1"), A("a1"), U("q2"), A("a2"), U("q3")],
        [run("r1"), run("r2", "running")],
      ),
    ).toBeNull();
  });

  it("all-terminal threads keep the strict D1 equality (no tolerance window)", () => {
    expect(
      buildHistoryTurns([U("q1"), A("a1")], [run("r1"), run("r2")]),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// run_id grouping — exact pairing when every message carries its owning run
// ---------------------------------------------------------------------------

const Ur = (content: string, runId: string): HistoryMessage => ({
  role: "user",
  content,
  channel: null,
  run_id: runId,
});
const Ar = (
  content: string,
  runId: string,
  channel: HistoryMessage["channel"] = null,
): HistoryMessage => ({ role: "assistant", content, channel, run_id: runId });

describe("buildHistoryTurns run_id grouping", () => {
  it("groups each run's messages by run_id, not by position", () => {
    // r2's rows sit BEFORE r1's in the flat list — order pairing would put
    // "q2" on r1. Only real grouping gets this right.
    const turns = buildHistoryTurns(
      [Ur("q2", "r2"), Ar("a2", "r2", "final"), Ur("q1", "r1"), Ar("a1", "r1", "final")],
      [run("r1"), run("r2")],
    );
    expect(turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [{ text: "a1", channel: "final" }],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
      {
        key: "r2",
        input: "q2",
        fallbackLines: [{ text: "a2", channel: "final" }],
        runId: "r2",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
    ]);
  });

  it("gives a continuation run that owns no user message an empty input instead of degrading", () => {
    // The approval case order pairing cannot express: r1 paused holding q1,
    // r2 resumed the SAME checkpoint (no user message of its own) and
    // answered. Both terminal, so the D-5 trailing window does not apply —
    // today this whole page falls back to flat text.
    const turns = buildHistoryTurns(
      [Ur("q1", "r1"), Ar("我先查一下", "r1", "commentary"), Ar("查到了", "r2", "final")],
      [run("r1"), run("r2")],
    );
    expect(turns).not.toBeNull();
    expect(turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [{ text: "我先查一下", channel: "commentary" }],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
      {
        key: "r2",
        input: "",
        fallbackLines: [{ text: "查到了", channel: "final" }],
        runId: "r2",
        status: "success",
        tokens: null,
        createdAt: "2026-01-01",
      },
    ]);
  });

  it("gives a run with no messages at all an empty turn instead of degrading", () => {
    // A just-spawned run whose messages are not checkpointed yet — the same
    // shape the D-5 trailing window produces, now without needing the run to
    // be trailing or non-terminal.
    const turns = buildHistoryTurns([Ur("q1", "r1"), Ar("a1", "r1")], [run("r1"), run("r2")]);
    expect(turns?.[1]).toEqual({
      key: "r2",
      input: "",
      fallbackLines: [],
      runId: "r2",
      status: "success",
      tokens: null,
      createdAt: "2026-01-01",
    });
  });

  it("ignores messages whose run is not in ``runs`` (they belong to another page)", () => {
    const turns = buildHistoryTurns(
      [Ur("q1", "r1"), Ar("a1", "r1"), Ur("其他 run 的", "r9"), Ar("其他 run 的答", "r9")],
      [run("r1")],
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
    ]);
  });

  it("carries status / tokens / createdAt from the run, not from the messages", () => {
    const tokens = {
      input_tokens: 10, output_tokens: 5, cache_creation_tokens: 0,
      cache_read_tokens: 0, total_tokens: 15, llm_calls: 1, models: ["m"],
    };
    const turns = buildHistoryTurns(
      [Ur("q1", "r1"), Ar("a1", "r1")],
      [{ runId: "r1", status: "running", isResume: true, createdAt: "2026-02-02", finishedAt: null, error: null, tokens }],
    );
    expect(turns?.[0]).toMatchObject({
      key: "r1", runId: "r1", status: "running", tokens, createdAt: "2026-02-02",
    });
  });

  it("collects every non-user message of a run as its own fallback line", () => {
    const turns = buildHistoryTurns(
      [Ur("q1", "r1"), Ar("a1", "r1"), Ar("a2", "r1"), Ar("a3", "r1")],
      [run("r1")],
    );
    expect(turns?.[0]?.fallbackLines).toEqual([
      { text: "a1", channel: null },
      { text: "a2", channel: null },
      { text: "a3", channel: null },
    ]);
  });
});

// ---------------------------------------------------------------------------
// run_id grouping — the fallbacks to order pairing
// ---------------------------------------------------------------------------

describe("buildHistoryTurns falls back to order pairing", () => {
  it("falls back when ONE message lacks run_id, reproducing the order-paired output", () => {
    const mixed: HistoryMessage[] = [
      { role: "user", content: "q1", channel: null }, // 盖戳上线前写入
      Ar("a1", "r1"),
      Ur("q2", "r2"),
      Ar("a2", "r2"),
    ];
    const expected = buildHistoryTurns([U("q1"), A("a1"), U("q2"), A("a2")], [run("r1"), run("r2")]);
    expect(buildHistoryTurns(mixed, [run("r1"), run("r2")])).toEqual(expected);
    expect(expected?.[0]?.input).toBe("q1");
  });

  it("falls back when a message carries run_id: null explicitly", () => {
    const mixed: HistoryMessage[] = [
      { role: "user", content: "q1", channel: null, run_id: null },
      Ar("a1", "r1"),
    ];
    expect(buildHistoryTurns(mixed, [run("r1")])).toEqual(
      buildHistoryTurns([U("q1"), A("a1")], [run("r1")]),
    );
  });

  it("a mixed thread still degrades to null when order pairing cannot reconcile", () => {
    // The null-run_id row keeps this on the order path, where 1 user turn vs
    // 2 terminal runs is the pre-existing degrade case.
    const mixed: HistoryMessage[] = [
      { role: "user", content: "q1", channel: null },
      Ar("a1", "r1"),
    ];
    expect(buildHistoryTurns(mixed, [run("r1"), run("r2")])).toBeNull();
  });

  it("falls back when one run owns two user rows (faithful cross-tenant audit view)", () => {
    // ``include_hidden=True`` keeps the orchestrator's ``<recovery-advisory>``
    // HumanMessage, which is stamped with the SAME run as the real input —
    // "the run's user message" is then ambiguous. Order pairing degrades to
    // flat text, which shows both rows; grouping would have to drop one.
    const turns = buildHistoryTurns(
      [Ur("q1", "r1"), Ur("<recovery-advisory>internal</recovery-advisory>", "r1"), Ar("a1", "r1")],
      [run("r1")],
    );
    expect(turns).toBeNull();
  });

  it("keeps the order path for an empty message list (nothing to group)", () => {
    // A best-effort transcript read that degraded to [] must not silently
    // switch strategy and render an empty-input card per run.
    expect(buildHistoryTurns([], [run("r1")])).toBeNull();
    expect(buildHistoryTurns([], [])).toEqual([]);
  });
});
