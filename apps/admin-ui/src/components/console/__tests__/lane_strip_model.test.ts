import { describe, expect, it } from "vitest";

import type { SseEvent } from "../../../api/sessions";
import { trajectoryRowsOf } from "../../../api/trajectory_rows";
import { laneModelOf } from "../lane_strip_model";

// serverMsOf requires a 10+-digit ms segment (real epoch ms) — fixtures use
// small offsets onto a realistic epoch base so ids parse as valid, same
// convention as api/__tests__/gantt_timeline.test.ts.
const BASE_MS = 1_700_000_000_000;
let seqCounter = 0;

function ev(event: string, data: unknown, ms: number | null, receivedAt = "2026-01-01T00:00:00.000Z"): SseEvent {
  seqCounter += 1;
  return {
    id: ms === null ? null : `${BASE_MS + ms}-${seqCounter}`,
    event,
    data,
    rawData: "",
    receivedAt,
  };
}
function upd(node: string, channels: Record<string, unknown>, ms: number | null, receivedAt?: string): SseEvent {
  return ev("updates", { [node]: channels }, ms, receivedAt);
}

const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

describe("laneModelOf", () => {
  it("maps gantt kinds to lanes and resolves each block to a trajectory row id", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            additional_kwargs: { reasoning_content: "先查客户档案" },
            tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
          },
        ],
      }, 1000),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }],
      }, 2000),
      upd("memory_recall", {
        recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }],
      }, 3000),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "done");
    const model = laneModelOf(events, rows, { running: false, nowMs: Date.now() });

    expect(model.blocks).toHaveLength(3);
    const byLane = new Map(model.blocks.map((b) => [b.lane, b]));
    expect(byLane.get("model")?.rowId).toBe("think:0");
    expect(byLane.get("tools")?.rowId).toBe("tool:0:0");
    expect(byLane.get("input")?.rowId).toBe("memory:1");
  });

  it("running: totalMs grows to now using the last frame's server ms + receivedAt delta", () => {
    const R = "2026-01-01T00:00:00.000Z";
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 500,
        messages: [{ type: "ai", content: "", tool_calls: [{ id: "c1", name: "t1", args: {} }] }],
      }, 1000, R),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "c1", name: "t1", content: "ok", status: "success" }],
      }, 2000, R),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "running");

    const settledTotalMs = laneModelOf(events, rows, { running: false, nowMs: 0 }).totalMs;
    const nowMs = Date.parse(R) + 5000;
    const runningTotalMs = laneModelOf(events, rows, { running: true, nowMs }).totalMs;

    expect(runningTotalMs).toBe(settledTotalMs + 5000);
  });

  it("markers are carried over with kind/text; degraded flag passes through", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 500,
        messages: [{ type: "ai", content: "", tool_calls: [] }],
      }, 1000),
      ev("compaction", { passes: 1, tokens_before: 100, tokens_after: 50, summary_chars: 10 }, 2000),
      upd("agent", {
        step_count: 2,
        messages: [{ type: "ai", content: "", tool_calls: [] }],
      }, null),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "done");
    const model = laneModelOf(events, rows, { running: false, nowMs: Date.now() });

    expect(model.degraded).toBe(true);
    expect(model.markers).toHaveLength(1);
    expect(model.markers[0]).toMatchObject({ kind: "compaction", text: "压缩 1 遍 · 100 → 50 tok" });
    expect(model.markers[0].key).toBe(`compaction-${model.markers[0].atMs}-0`);
  });
});
