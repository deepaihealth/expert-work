/**
 * B-222 PR-2 — pure helpers for an ingest's estimated and actual cost.
 *
 * Amounts are integer micro-yuan (¥1 = 1,000,000). Shown to the cent: below a
 * cent but above zero is 「< ¥0.01」, zero is 「¥0.00」 (still to be confirmed).
 */
import type { IngestEstimate, KnowledgeDocument } from "../../api/knowledge";

const MICROS_PER_CENT = 10_000;
const DAY_MS = 24 * 60 * 60 * 1000;

/** ``¥0.12`` / ``< ¥0.01`` / ``¥0.00``. */
export function formatYuan(micros: number): string {
  if (micros > 0 && micros < MICROS_PER_CENT) return "< ¥0.01";
  return `¥${(Math.round(micros / MICROS_PER_CENT) / 100).toFixed(2)}`;
}

/** Whole days left until ``confirmBy``, rounded up (0 once it has passed). */
export function daysLeft(confirmBy: string | null | undefined, now: number = Date.now()): number {
  if (!confirmBy) return 0;
  const left = new Date(confirmBy).getTime() - now;
  return left <= 0 ? 0 : Math.ceil(left / DAY_MS);
}

/** A duplicate (K-21 ①): another document of the base holds these bytes. */
export function isDuplicate(estimate: IngestEstimate | null | undefined): boolean {
  return Boolean(estimate?.duplicate_of) || estimate?.estimated === false;
}

/** The document's estimate can be confirmed as it is (¥0.00 included). */
export function canConfirm(document: KnowledgeDocument): boolean {
  return (
    document.status === "awaiting_confirmation" &&
    Boolean(document.ingest_estimate) &&
    !isDuplicate(document.ingest_estimate)
  );
}

/** What the latest paid ingest cost, when it was the one confirmed under the
 *  document's current estimate (else the two would not belong together). */
export function matchingActual(document: KnowledgeDocument): number | null {
  const actual = document.ingest_actual;
  if (!actual) return null;
  const estimateId = document.ingest_estimate?.id ?? null;
  return actual.estimate_id === estimateId ? actual.total_micros : null;
}

/** Statuses a document waits in before anything is spent. */
export function isWaiting(status: KnowledgeDocument["status"]): boolean {
  return status === "estimating" || status === "awaiting_confirmation";
}
