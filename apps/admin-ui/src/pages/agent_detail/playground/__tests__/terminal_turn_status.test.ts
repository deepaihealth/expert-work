/**
 * BUG-9 (live path, 终审 F1) — ``terminalTurnStatus``: the LIVE turn's
 * terminal status comes from the backend ``end`` frame's ``status``, so a
 * cancelled run finalizes as「已中断」in the same session, matching what the
 * replayed history view shows after a reload.
 */
import { describe, expect, it } from "vitest";

import type { SseEvent } from "../../../../api/sessions";
import { terminalTurnStatus } from "../useRunEngine";

const ev = (event: string, data: unknown): SseEvent =>
  ({ id: "1", event, data, rawData: "", receivedAt: "" }) as SseEvent;

describe("terminalTurnStatus", () => {
  it("maps an interrupted end frame to 'interrupted'", () => {
    expect(
      terminalTurnStatus([ev("metadata", { run_id: "r" }), ev("end", { status: "interrupted" })]),
    ).toBe("interrupted");
  });

  it("keeps 'done' for success / paused / unknown end statuses", () => {
    expect(terminalTurnStatus([ev("end", { status: "success" })])).toBe("done");
    expect(terminalTurnStatus([ev("end", { status: "paused" })])).toBe("done");
    expect(terminalTurnStatus([ev("end", null)])).toBe("done");
  });

  it("keeps 'done' when no end frame arrived", () => {
    expect(terminalTurnStatus([ev("metadata", { run_id: "r" })])).toBe("done");
  });

  it("reads the LAST end frame (a resumed stream can replay an earlier one)", () => {
    expect(
      terminalTurnStatus([ev("end", { status: "success" }), ev("end", { status: "interrupted" })]),
    ).toBe("interrupted");
  });
});
