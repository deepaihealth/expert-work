/** B-222 PR-2 — money and deadline formatting of an ingest estimate. */
import { describe, expect, it } from "vitest";

import type { IngestEstimate, KnowledgeDocument } from "../../../api/knowledge";
import { canConfirm, daysLeft, formatYuan, isDuplicate, matchingActual } from "../ingestCost";

function estimate(extra: Partial<IngestEstimate> = {}): IngestEstimate {
  return {
    id: "e1",
    origin: "upload",
    estimated: true,
    created_at: "2026-10-28T00:00:00Z",
    duplicate_of: null,
    allow_duplicate: false,
    currency: "CNY",
    total_micros: 0,
    unpriced: false,
    notes: [],
    counts: { chunks: 1, scanned_pages: 0, figures: 0, pages_over_limit: 0 },
    items: [],
    confirmed_at: null,
    confirmed_by: null,
    ...extra,
  };
}

function document(extra: Partial<KnowledgeDocument> = {}): KnowledgeDocument {
  return {
    id: "d1",
    filename: "a.md",
    status: "awaiting_confirmation",
    error: null,
    chunk_count: 0,
    created_at: null,
    updated_at: null,
    ingest_estimate: estimate(),
    ...extra,
  };
}

describe("formatYuan", () => {
  it.each([
    [0, "¥0.00"],
    [1, "< ¥0.01"],
    [9_999, "< ¥0.01"],
    [10_000, "¥0.01"],
    [120_000, "¥0.12"],
    [1_234_567, "¥1.23"],
  ])("%d micro-yuan reads %s", (micros, shown) => {
    expect(formatYuan(micros)).toBe(shown);
  });
});

describe("daysLeft", () => {
  const now = Date.parse("2026-10-28T00:00:00Z");
  it("rounds up and stops at zero", () => {
    expect(daysLeft("2026-11-04T00:00:00Z", now)).toBe(7);
    expect(daysLeft("2026-10-28T01:00:00Z", now)).toBe(1);
    expect(daysLeft("2026-10-27T23:00:00Z", now)).toBe(0);
    expect(daysLeft(null, now)).toBe(0);
  });
});

describe("what can be confirmed", () => {
  it("an estimate of ¥0.00 still needs (and allows) a confirmation", () => {
    expect(canConfirm(document())).toBe(true);
  });
  it("a duplicate is ingested anyway first, not confirmed", () => {
    const copy = estimate({ duplicate_of: { id: "d0", doc_seq: 12, filename: "x.md" } });
    expect(isDuplicate(copy)).toBe(true);
    expect(isDuplicate(estimate({ estimated: false }))).toBe(true);
    expect(canConfirm(document({ ingest_estimate: copy }))).toBe(false);
    expect(canConfirm(document({ status: "estimating" }))).toBe(false);
  });
});

describe("matchingActual", () => {
  const actual = {
    estimate_id: "e1",
    generation: 2,
    currency: "CNY",
    total_micros: 50_000,
    unpriced: false,
    outcome: "ready" as const,
    finished_at: "2026-10-28T00:10:00Z",
  };
  it("is the actual of the ingest confirmed under the current estimate", () => {
    expect(matchingActual(document({ status: "ready", ingest_actual: actual }))).toBe(50_000);
    const other = { ...actual, estimate_id: "e0" };
    expect(matchingActual(document({ status: "ready", ingest_actual: other }))).toBeNull();
    expect(matchingActual(document({ status: "ready", ingest_actual: null }))).toBeNull();
  });
});
