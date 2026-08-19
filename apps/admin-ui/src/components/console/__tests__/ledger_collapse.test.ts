/**
 * ledger_collapse — turn/owner collapsing, the two summary-row shapes, and
 * the two "what can be collapsed" queries. Fixtures are hand-built minimal
 * ``LedgerRecord``/``LedgerTurn`` objects (cast, per the task-4 brief)
 * rather than routing through ``ledger.ts``'s ``buildLedger``.
 */
import { describe, expect, it } from "vitest";

import {
  collapsibleOwnerIds,
  collapsibleTurnKeys,
  displayRowsOf,
  turnSummaryOf,
} from "../ledger_collapse";
import type { Ledger, LedgerRecord, LedgerTurn } from "../ledger_types";

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

function turn(overrides: Partial<LedgerTurn> & { key: string }): LedgerTurn {
  return {
    seq: 0,
    runId: null,
    status: "done",
    firstIndex: 0,
    lastIndex: 0,
    requestNos: [],
    ...overrides,
  } as LedgerTurn;
}

function ledgerOf(records: LedgerRecord[], turns: LedgerTurn[]): Ledger {
  return { records, requests: [], turns, timed: false };
}

describe("displayRowsOf", () => {
  it("no collapse → one record row per record in order", () => {
    const records = [
      rec({ id: "u1", index: 0, turnKey: "t1", kind: "user" }),
      rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant" }),
      rec({ id: "u2", index: 2, turnKey: "t2", kind: "user" }),
      rec({ id: "a2", index: 3, turnKey: "t2", kind: "assistant" }),
    ];
    const ledger = ledgerOf(records, [turn({ key: "t1" }), turn({ key: "t2" })]);

    const rows = displayRowsOf(ledger, { collapsedTurns: new Set(), collapsedOwners: new Set(), matches: null });

    expect(rows).toEqual(records.map((record) => ({ kind: "record", record })));
  });

  it("collapsed turn → USER row + one turn-summary with process counts / failed / duration", () => {
    const user = rec({ id: "u1", index: 0, turnKey: "t1", kind: "user", turnStart: true });
    const a1 = rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant", startedAt: 1000, endedAt: 1200 });
    const t1 = rec({
      id: "tool1",
      index: 2,
      turnKey: "t1",
      kind: "tool",
      parentId: "a1",
      isError: true,
      startedAt: 1200,
      endedAt: 1300,
      row: { kind: "tool", entry: { toolName: "bash", server: null } } as LedgerRecord["row"],
    });
    const a2 = rec({ id: "a2", index: 3, turnKey: "t1", kind: "assistant", startedAt: 1300, endedAt: 1400 });
    const records = [user, a1, t1, a2];
    const ledger = ledgerOf(records, [turn({ key: "t1", seq: 0, runId: "run-1" })]);

    const rows = displayRowsOf(ledger, {
      collapsedTurns: new Set(["t1"]),
      collapsedOwners: new Set(),
      matches: null,
    });

    expect(rows).toEqual([
      { kind: "record", record: user },
      {
        kind: "turn-summary",
        turnKey: "t1",
        turnSeq: 0,
        runId: "run-1",
        summary: { think: 2, tools: 1, other: 0, failed: 1, toolBreakdown: "bash ×1", durationMs: 400 },
        hasError: true,
        anchorIndex: 0,
      },
    ]);
  });

  it("collapsed owner → assistant row + one calls-summary listing children by tool name ×count; children hidden", () => {
    const user = rec({ id: "u1", index: 0, turnKey: "t1", kind: "user" });
    const a1 = rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant" });
    const bash1 = rec({
      id: "bash1", index: 2, turnKey: "t1", kind: "tool", parentId: "a1",
      row: { kind: "tool", entry: { toolName: "bash", server: null } } as LedgerRecord["row"],
    });
    const bash2 = rec({
      id: "bash2", index: 3, turnKey: "t1", kind: "tool", parentId: "a1",
      row: { kind: "tool", entry: { toolName: "bash", server: null } } as LedgerRecord["row"],
    });
    const readFile = rec({
      id: "read1", index: 4, turnKey: "t1", kind: "tool", parentId: "a1",
      row: { kind: "tool", entry: { toolName: "read_file", server: null } } as LedgerRecord["row"],
    });
    const records = [user, a1, bash1, bash2, readFile];
    const ledger = ledgerOf(records, [turn({ key: "t1" })]);

    const rows = displayRowsOf(ledger, {
      collapsedTurns: new Set(),
      collapsedOwners: new Set(["a1"]),
      matches: null,
    });

    expect(rows).toEqual([
      { kind: "record", record: user },
      { kind: "record", record: a1 },
      { kind: "calls-summary", ownerId: "a1", turnKey: "t1", count: 3, toolBreakdown: "bash ×2 · read_file ×1" },
    ]);
  });

  it("matches present → only matching records, collapse ignored", () => {
    const records = [
      rec({ id: "u1", index: 0, turnKey: "t1", kind: "user" }),
      rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant" }),
      rec({ id: "tool1", index: 2, turnKey: "t1", kind: "tool", parentId: "a1" }),
      rec({ id: "a2", index: 3, turnKey: "t1", kind: "assistant" }),
    ];
    const ledger = ledgerOf(records, [turn({ key: "t1" })]);

    const rows = displayRowsOf(ledger, {
      // Both collapse sets are non-empty, to prove `matches` overrides them.
      collapsedTurns: new Set(["t1"]),
      collapsedOwners: new Set(["a1"]),
      matches: new Set([1, 3]),
    });

    expect(rows).toEqual([
      { kind: "record", record: records[1] },
      { kind: "record", record: records[3] },
    ]);
  });

  it("subagent under a collapsed owner is hidden too (parent tool belongs to that owner)", () => {
    const user = rec({ id: "u1", index: 0, turnKey: "t1", kind: "user" });
    const a1 = rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant" });
    const tool1 = rec({
      id: "tool1", index: 2, turnKey: "t1", kind: "tool", parentId: "a1",
      row: { kind: "tool", entry: { toolName: "exec_python", server: null } } as LedgerRecord["row"],
    });
    const tool2 = rec({
      id: "tool2", index: 3, turnKey: "t1", kind: "tool", parentId: "a1",
      row: { kind: "tool", entry: { toolName: "exec_python", server: null } } as LedgerRecord["row"],
    });
    const sub = rec({
      id: "sub1", index: 4, turnKey: "t1", kind: "subagent", parentId: "tool1",
      row: { kind: "subagent", worker: { label: "worker_a" } } as LedgerRecord["row"],
    });
    const records = [user, a1, tool1, tool2, sub];
    const ledger = ledgerOf(records, [turn({ key: "t1" })]);

    const rows = displayRowsOf(ledger, {
      collapsedTurns: new Set(),
      collapsedOwners: new Set(["a1"]),
      matches: null,
    });

    expect(rows).toEqual([
      { kind: "record", record: user },
      { kind: "record", record: a1 },
      { kind: "calls-summary", ownerId: "a1", turnKey: "t1", count: 3, toolBreakdown: "exec_python ×2 · worker_a ×1" },
    ]);
    expect(rows.some((r) => r.kind === "record" && r.record.id === "tool1")).toBe(false);
    expect(rows.some((r) => r.kind === "record" && r.record.id === "sub1")).toBe(false);
  });
});

describe("turnSummaryOf", () => {
  it("counts assistant as think and excludes assistant errors from failed", () => {
    const records = [
      rec({ id: "a1", index: 0, kind: "assistant", isError: true }),
      rec({ id: "a2", index: 1, kind: "assistant", isError: false }),
      rec({
        id: "t1", index: 2, kind: "tool", isError: true,
        row: { kind: "tool", entry: { toolName: "bash", server: null } } as LedgerRecord["row"],
      }),
    ];

    expect(turnSummaryOf(records)).toMatchObject({ think: 2, tools: 1, failed: 1 });
  });

  it("toolBreakdown ties (equal counts) sort alphabetically by name", () => {
    const records = [
      rec({ id: "t1", index: 0, kind: "tool", row: { kind: "tool", entry: { toolName: "bravo", server: null } } as LedgerRecord["row"] }),
      rec({ id: "t2", index: 1, kind: "tool", row: { kind: "tool", entry: { toolName: "alpha", server: null } } as LedgerRecord["row"] }),
      rec({ id: "t3", index: 2, kind: "tool", row: { kind: "tool", entry: { toolName: "bravo", server: null } } as LedgerRecord["row"] }),
      rec({ id: "t4", index: 3, kind: "tool", row: { kind: "tool", entry: { toolName: "alpha", server: null } } as LedgerRecord["row"] }),
    ];

    // Both names appear twice — count alone can't order them, so the
    // localeCompare tie-break must kick in ("alpha" before "bravo").
    expect(turnSummaryOf(records).toolBreakdown).toBe("alpha ×2 · bravo ×2");
  });
});

describe("collapsibleTurnKeys / collapsibleOwnerIds", () => {
  it("collapsibleTurnKeys excludes single-record turns; collapsibleOwnerIds lists assistants with children", () => {
    const t1User = rec({ id: "u1", index: 0, turnKey: "t1", kind: "user" });
    const t1Assistant = rec({ id: "a1", index: 1, turnKey: "t1", kind: "assistant" });
    const t2User = rec({ id: "u2", index: 2, turnKey: "t2", kind: "user" });
    const t2Assistant = rec({ id: "a2", index: 3, turnKey: "t2", kind: "assistant" });
    const t2Tool = rec({ id: "tool2", index: 4, turnKey: "t2", kind: "tool", parentId: "a2" });
    const records = [t1User, t1Assistant, t2User, t2Assistant, t2Tool];
    const ledger = ledgerOf(records, [turn({ key: "t1" }), turn({ key: "t2" })]);

    // t1 has exactly one non-user record (the assistant) → not collapsible.
    // t2 has two (assistant + tool) → collapsible.
    expect(collapsibleTurnKeys(ledger)).toEqual(["t2"]);
    // a1 owns no children; a2 owns the tool call.
    expect(collapsibleOwnerIds(ledger)).toEqual(["a2"]);
  });
});
