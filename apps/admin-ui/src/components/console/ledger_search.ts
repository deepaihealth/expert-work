/**
 * ledger_search — free-text search over the trajectory ledger's flat record
 * list (spec §九). Whitespace-split terms, every term must hit somewhere in
 * a record's kind label / text / result preview / tool name (case-
 * insensitive); a blank query means "no filter" (``null``), a non-blank
 * query with zero hits is an *empty* set (search is active, nothing
 * matched). Pure function, no rendering, no state. 交互参照 deepseek-harness
 * ui-trajectory(MIT)重写. See
 * .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-4-brief.md.
 */
import type { LedgerRecord } from "./ledger_types";

/** Search-facing kind label: ``TOOL`` / ``ASSISTANT`` / …, but ``subagent``
 *  reads as ``SUBTOOL`` and ``compaction`` as ``COMPACTED``. */
function kindLabel(kind: LedgerRecord["kind"]): string {
  if (kind === "subagent") return "SUBTOOL";
  if (kind === "compaction") return "COMPACTED";
  return kind.toUpperCase();
}

/** Lower-cased haystack a query term is matched against. */
function haystackOf(record: LedgerRecord): string {
  const parts = [kindLabel(record.kind), record.text, record.resultText ?? ""];
  if (record.row.kind === "tool") {
    parts.push(record.row.entry.toolName, record.row.entry.server ?? "");
  }
  return parts.join(" ").toLowerCase();
}

/** Blank/whitespace query → ``null`` (no filter). Otherwise every
 *  whitespace-split, case-insensitive term must hit the record's haystack;
 *  returns the set of matching record ``index``es (possibly empty). */
export function searchLedger(
  records: readonly LedgerRecord[],
  query: string,
): ReadonlySet<number> | null {
  const terms = query
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((t) => t.toLowerCase());
  if (terms.length === 0) return null;

  const hits = new Set<number>();
  for (const record of records) {
    const haystack = haystackOf(record);
    if (terms.every((term) => haystack.includes(term))) hits.add(record.index);
  }
  return hits;
}
