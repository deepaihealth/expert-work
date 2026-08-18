/**
 * relativeTime — lifted verbatim out of SessionHistoryDrawer (PR-A Task 6).
 * Pins the three buckets closest to a caller: just now / minutes / days.
 */
import { describe, expect, it } from "vitest";

import { relativeTime } from "../relative_time";

/** Stub translator recording the key + interpolation opts it was called
 *  with, so assertions don't depend on any particular locale's copy. */
function fakeT(key: string, opts?: Record<string, unknown>): string {
  return opts ? `${key}:${JSON.stringify(opts)}` : key;
}

describe("relativeTime", () => {
  it("just now — under a minute old", () => {
    const iso = new Date(Date.now() - 5_000).toISOString();
    expect(relativeTime(iso, fakeT)).toBe("session_history.time_now");
  });

  it("N minutes — rounds down to whole minutes", () => {
    const iso = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(relativeTime(iso, fakeT)).toBe(
      'session_history.time_minutes:{"n":5}',
    );
  });

  it("N days — rounds down to whole days, under a week", () => {
    const iso = new Date(Date.now() - 3 * 24 * 3_600_000).toISOString();
    expect(relativeTime(iso, fakeT)).toBe('session_history.time_days:{"n":3}');
  });
});
