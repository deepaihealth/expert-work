/**
 * ledger_collapse — turns a ``Ledger`` into the flat list of rows the
 * ledger table actually renders (spec §九): plain record rows, or — when
 * the caller has collapsed a turn / an assistant's tool calls and no
 * search filter is active — a summarized stand-in row. Pure, no rendering,
 * no state. 折叠模型参照 deepseek-harness ui-trajectory(MIT)重写. See
 * .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-4-brief.md.
 */
import type { Ledger, LedgerRecord } from "./ledger_types";
import type { ProcessSummary } from "./process_summary";

/** 「上下文行」谓词的唯一来源(PR-A.3 §十.1)—— USER 与 SYSTEM 都是上下文,
 *  不是 agent 迈出的一步:折叠轮时保留、`turnSummaryOf` 不计进 other、
 *  `collapsibleTurnKeys` 的「≥2 条」门槛不数它们。别在别处再写裸
 *  `=== "user"` / `!== "user"` 判断。 */
export const CONTEXT_KINDS: ReadonlySet<LedgerRecord["kind"]> = new Set(["user", "system"]);

export type DisplayRow =
  | { kind: "record"; record: LedgerRecord }
  | {
      kind: "turn-summary";
      turnKey: string;
      turnSeq: number;
      runId: string | null;
      summary: ProcessSummary;
      hasError: boolean;
      anchorIndex: number;
    }
  | { kind: "calls-summary"; ownerId: string; turnKey: string; count: number; toolBreakdown: string };

/** "name ×count · name ×count", sorted by count desc then name asc; ""
 *  when ``labels`` is empty. Shared format for both summary rows. */
function breakdownOf(labels: readonly string[]): string {
  const counts = new Map<string, number>();
  for (const label of labels) counts.set(label, (counts.get(label) ?? 0) + 1);
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, count]) => `${name} ×${count}`)
    .join(" · ");
}

/** A tool/plan/subagent child's breakdown label ("bash", "update_plan", the
 *  subagent's worker label) — ``null`` for kinds that never own children. */
function childLabel(record: LedgerRecord): string | null {
  if (record.row.kind === "tool") return record.row.entry.toolName;
  if (record.row.kind === "plan") return "update_plan";
  if (record.row.kind === "subagent") return record.row.worker.label;
  return null;
}

/** ``owner``'s tool/plan children (``parentId === owner.id``) plus any
 *  subagent whose parent tool belongs to ``owner`` (one hop through the
 *  tool's own ``parentId``). Only those kinds — reflect / memory-writeback
 *  records also carry a ``parentId`` (details-panel hierarchy link), but they
 *  are the step's after-products, not calls, and stay visible when the owner's
 *  calls are collapsed. */
function childrenOf(records: readonly LedgerRecord[], ownerId: string): LedgerRecord[] {
  const direct = records.filter(
    (r) => r.parentId === ownerId && (r.kind === "tool" || r.kind === "plan"),
  );
  if (direct.length === 0) return direct;
  const directIds = new Set(direct.map((r) => r.id));
  const transitive = records.filter(
    (r) => r.kind === "subagent" && r.parentId !== null && directIds.has(r.parentId),
  );
  return [...direct, ...transitive];
}

/** A turn's process summary: assistant records count as "think" (spec §九
 *  — one ASSISTANT record per step replaces the compact THINK row), tool
 *  records count as "tools" (and feed ``toolBreakdown``), every other
 *  non-context kind (``CONTEXT_KINDS`` — not user, not the PR-A.3 SYSTEM
 *  row) counts as "other". ``failed`` is the non-assistant
 *  ``isError`` count; ``durationMs`` is the turn's wall clock —
 *  ``max(endedAt) - min(startedAt)`` over the records that have both
 *  timestamps (``null`` when none do). Summing each record's own span
 *  instead would count parallel tool calls twice over. */
export function turnSummaryOf(records: readonly LedgerRecord[]): ProcessSummary {
  let think = 0;
  let tools = 0;
  let other = 0;
  let failed = 0;
  let first: number | null = null;
  let last: number | null = null;
  const toolLabels: string[] = [];

  for (const r of records) {
    if (r.kind === "assistant") think += 1;
    else if (r.kind === "tool") {
      tools += 1;
      if (r.row.kind === "tool") toolLabels.push(r.row.entry.toolName);
    } else if (!CONTEXT_KINDS.has(r.kind)) other += 1;

    if (r.kind !== "assistant" && r.isError) failed += 1;
    if (r.startedAt !== null && r.endedAt !== null) {
      first = first === null ? r.startedAt : Math.min(first, r.startedAt);
      last = last === null ? r.endedAt : Math.max(last, r.endedAt);
    }
  }

  return {
    think,
    tools,
    other,
    failed,
    toolBreakdown: breakdownOf(toolLabels),
    durationMs: first === null || last === null ? null : last - first,
  };
}

/** Turns with ≥ 2 non-context (``CONTEXT_KINDS``) records — the only ones
 *  worth collapsing. */
export function collapsibleTurnKeys(ledger: Ledger): string[] {
  const nonUserCounts = new Map<string, number>();
  for (const r of ledger.records) {
    if (CONTEXT_KINDS.has(r.kind)) continue;
    nonUserCounts.set(r.turnKey, (nonUserCounts.get(r.turnKey) ?? 0) + 1);
  }
  return ledger.turns.filter((t) => (nonUserCounts.get(t.key) ?? 0) >= 2).map((t) => t.key);
}

/** Assistant records that own ≥ 1 child (direct tool/plan, or a subagent
 *  one hop through its parent tool) — the only ones worth collapsing. */
export function collapsibleOwnerIds(ledger: Ledger): string[] {
  const ids: string[] = [];
  for (const r of ledger.records) {
    if (r.kind !== "assistant") continue;
    if (childrenOf(ledger.records, r.id).length > 0) ids.push(r.id);
  }
  return ids;
}

/** Flattens the ledger into display rows. A search filter (``matches``
 *  non-null) wins outright: only matching records show, uncollapsed. Else
 *  a collapsed turn becomes its context record(s) (USER / SYSTEM,
 *  ``CONTEXT_KINDS``) + one ``turn-summary``; a collapsed owner's
 *  tool/plan/subagent children are hidden behind one ``calls-summary`` row
 *  right after the owner's own record. */
export function displayRowsOf(
  ledger: Ledger,
  opts: {
    collapsedTurns: ReadonlySet<string>;
    collapsedOwners: ReadonlySet<string>;
    matches: ReadonlySet<number> | null;
  },
): DisplayRow[] {
  const { collapsedTurns, collapsedOwners, matches } = opts;

  if (matches) {
    return ledger.records
      .filter((r) => matches.has(r.index))
      .map((record) => ({ kind: "record" as const, record }));
  }

  const byTurn = new Map<string, LedgerRecord[]>();
  for (const r of ledger.records) {
    const arr = byTurn.get(r.turnKey);
    if (arr) arr.push(r);
    else byTurn.set(r.turnKey, [r]);
  }

  // Precompute, once, every collapsed owner's children (id → children) so
  // per-record hiding and the owner's own calls-summary read the same set.
  const ownerChildren = new Map<string, LedgerRecord[]>();
  const hiddenIds = new Set<string>();
  for (const ownerId of collapsedOwners) {
    const children = childrenOf(ledger.records, ownerId);
    ownerChildren.set(ownerId, children);
    for (const c of children) hiddenIds.add(c.id);
  }

  const rows: DisplayRow[] = [];
  for (const turn of ledger.turns) {
    const turnRecords = byTurn.get(turn.key) ?? [];
    if (turnRecords.length === 0) continue;

    if (collapsedTurns.has(turn.key)) {
      for (const r of turnRecords) {
        if (CONTEXT_KINDS.has(r.kind)) rows.push({ kind: "record", record: r });
      }
      rows.push({
        kind: "turn-summary",
        turnKey: turn.key,
        turnSeq: turn.seq,
        runId: turn.runId,
        summary: turnSummaryOf(turnRecords),
        hasError: turnRecords.some((r) => r.isError),
        anchorIndex: turnRecords[0].index,
      });
      continue;
    }

    for (const record of turnRecords) {
      if (hiddenIds.has(record.id)) continue;
      rows.push({ kind: "record", record });
      if (record.kind === "assistant" && collapsedOwners.has(record.id)) {
        const children = ownerChildren.get(record.id) ?? [];
        if (children.length > 0) {
          const labels = children
            .map(childLabel)
            .filter((l): l is string => l !== null);
          rows.push({
            kind: "calls-summary",
            ownerId: record.id,
            turnKey: record.turnKey,
            count: children.length,
            toolBreakdown: breakdownOf(labels),
          });
        }
      }
    }
  }
  return rows;
}
