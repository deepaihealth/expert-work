/**
 * ledger_search — whitespace-split, every-term, case-insensitive matching
 * over a hand-built ``LedgerRecord[]`` (kept minimal and cast, per the
 * task-4 brief, rather than importing ``ledger.ts``'s ``buildLedger`` and
 * pulling in the other parallel task's fixture machinery).
 */
import { describe, expect, it } from "vitest";

import { searchLedger } from "../ledger_search";
import type { LedgerRecord } from "../ledger_types";

function rec(overrides: Partial<LedgerRecord> & { kind: LedgerRecord["kind"] }): LedgerRecord {
  return {
    id: "r0",
    index: 0,
    turnKey: "t1",
    turnSeq: 0,
    runId: null,
    turnStart: false,
    turnEnd: false,
    requestNo: null,
    ownerRequestNo: null,
    parentId: null,
    lane: 1,
    isError: false,
    running: false,
    startedAt: null,
    endedAt: null,
    text: "",
    resultText: null,
    row: { kind: overrides.kind },
    events: [],
    placeholder: null,
    ...overrides,
  } as LedgerRecord;
}

describe("searchLedger", () => {
  it("blank / whitespace query → null", () => {
    const records = [rec({ kind: "assistant", index: 0, text: "hello" })];
    expect(searchLedger(records, "")).toBeNull();
    expect(searchLedger(records, "   ")).toBeNull();
    expect(searchLedger(records, "\t\n ")).toBeNull();
  });

  it("matches are case-insensitive over kind label, text, resultText and tool name; every term must hit", () => {
    const records = [
      rec({
        kind: "tool",
        index: 0,
        text: "calls foo helper",
        resultText: "bar result",
        row: { kind: "tool", entry: { toolName: "mytool", server: null } } as LedgerRecord["row"],
      }),
      // Same "bar" hit, but missing the kind-label term — must not match.
      rec({ kind: "assistant", index: 1, text: "bar only, no matching label" }),
    ];

    // "TOOL" only hits record 0's kind label ("TOOL"); "bar" hits its
    // resultText. Every term must land on the same record.
    expect(searchLedger(records, "TOOL bar")).toEqual(new Set([0]));
    // Same query, mixed case — case-insensitivity on both the label and
    // the matched text.
    expect(searchLedger(records, "ToOl BaR")).toEqual(new Set([0]));
    // The tool name alone also hits record 0.
    expect(searchLedger(records, "mytool")).toEqual(new Set([0]));
  });

  it("subagent matches SUBTOOL and compaction matches COMPACTED", () => {
    const records = [
      rec({ kind: "subagent", index: 0, text: "" }),
      rec({ kind: "compaction", index: 1, text: "" }),
    ];
    expect(searchLedger(records, "subtool")).toEqual(new Set([0]));
    expect(searchLedger(records, "compacted")).toEqual(new Set([1]));
    // Neither's raw kind name is the label used for matching.
    expect(searchLedger(records, "subagent")).toEqual(new Set());
    expect(searchLedger(records, "compaction")).toEqual(new Set());
  });

  it("no hit → empty set (not null)", () => {
    const records = [rec({ kind: "assistant", index: 0, text: "hello world" })];
    const result = searchLedger(records, "nope");
    expect(result).not.toBeNull();
    expect(result).toEqual(new Set());
  });
});
