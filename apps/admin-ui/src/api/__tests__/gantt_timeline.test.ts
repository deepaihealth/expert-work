import { describe, expect, it } from "vitest";

import { buildGanttRows } from "../gantt_timeline";
import type { SseEvent } from "../sessions";

let seqCounter = 0;
// serverMsOf requires a 10+-digit ms segment (real epoch ms) — fixtures use
// small offsets (0, 200, 5000, …) for readable deltas, added onto a
// realistic epoch base so ids parse as valid.
const BASE_MS = 1_700_000_000_000;

/** Build an SSE frame whose ``id`` encodes the given server ms
 *  (``"{ms}-{seq}"``, matching live/replay's shared id shape) — or a
 *  ``null`` id when ``ms`` is ``null``, simulating a missing/malformed
 *  frame id. */
function ev(event: string, data: unknown, ms: number | null): SseEvent {
  seqCounter += 1;
  return {
    id: ms === null ? null : `${BASE_MS + ms}-${seqCounter}`,
    event,
    data,
    rawData: "",
    receivedAt: new Date().toISOString(),
  };
}
function upd(node: string, channels: Record<string, unknown>, ms: number | null): SseEvent {
  return ev("updates", { [node]: channels }, ms);
}
/** One agent-node ``updates`` channel: an AI message with the given step
 *  count, optional ``_duration_ms``, and optional tool_calls. */
function agentChannel(
  stepCount: number,
  durationMs: number | null,
  toolCalls: Array<{ id: string; name: string }> = [],
): Record<string, unknown> {
  return {
    step_count: stepCount,
    ...(durationMs !== null ? { _duration_ms: durationMs } : {}),
    messages: [
      {
        type: "ai",
        content: "",
        tool_calls: toolCalls.map((t) => ({ id: t.id, name: t.name, args: {} })),
      },
    ],
  };
}
/** A tool RESULT frame (``updates`` chunk carrying a ToolMessage). */
function toolResult(id: string, name: string, durationMs: number | null, ms: number | null): SseEvent {
  return upd(
    "tools",
    {
      messages: [
        {
          type: "tool",
          tool_call_id: id,
          name,
          content: "ok",
          status: "success",
          ...(durationMs !== null ? { additional_kwargs: { duration_ms: durationMs } } : {}),
        },
      ],
    },
    ms,
  );
}
function workerFrame(
  kind: "start" | "update" | "end",
  wseq: number,
  data: Record<string, unknown>,
  ms: number | null,
): SseEvent {
  return ev(
    "worker",
    {
      worker_id: "w-9",
      parent_worker_id: null,
      parent_tool_call_id: "call-9",
      label: "spawn_worker",
      agent_ref: "dynamic:general",
      depth: 1,
      kind,
      wseq,
      data,
    },
    ms,
  );
}

const fx = {
  // One agent step dispatching two tool calls that finish at the same
  // server ms but took different durations to run — their bars must
  // overlap (concurrent), not chain.
  concurrentTools: [
    upd("agent", agentChannel(1, 200, [
      { id: "c1", name: "tool_a" },
      { id: "c2", name: "tool_b" },
    ]), 1000),
    toolResult("c1", "tool_a", 5100, 5000),
    toolResult("c2", "tool_b", 4400, 5000),
  ] satisfies SseEvent[],

  // Two agent steps, both with a missing/malformed frame id (id: null) —
  // both rows must degrade to sequential placement, chained off each
  // other's end.
  noIds: [
    upd("agent", agentChannel(1, 1000), null),
    upd("agent", agentChannel(2, 500), null),
  ] satisfies SseEvent[],

  // A sub_agent tool call (spawn_worker) whose worker frames land a nested
  // step under it.
  delegation: [
    upd("agent", agentChannel(1, 300, [{ id: "call-9", name: "spawn_worker" }]), 1000),
    toolResult("call-9", "spawn_worker", 900, 2000),
    workerFrame("start", 0, { task_excerpt: "x", role: null, max_steps: 8 }, 1500),
    workerFrame("update", 1, { node: "agent", step_count: 1, _duration_ms: 300, messages: [] }, 1800),
    workerFrame(
      "end",
      2,
      { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 900 },
      1900,
    ),
  ] satisfies SseEvent[],

  // A settled turn: two agent steps, both with durations — the last one
  // becomes the terminal "final" row once settled.
  settledRun: [
    upd("agent", agentChannel(1, 500), 1000),
    upd("agent", agentChannel(2, 700), 2000),
  ] satisfies SseEvent[],

  // A still-running turn: the latest agent step has no ``_duration_ms`` yet
  // (in-progress row) — durationMs must stay null, kind must stay "agent".
  runningRun: [
    upd("agent", agentChannel(1, 500), 1000),
    upd("agent", agentChannel(2, null), 2000),
  ] satisfies SseEvent[],

  // A run with retry/error/end markers interleaved with one agent step —
  // markers must not become rows.
  withMarkers: [
    upd("agent", agentChannel(1, 500), 1000),
    ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }, 1500),
    ev("error", { message: "boom" }, 1600),
    ev("end", {}, 1700),
  ] satisfies SseEvent[],
};

describe("buildGanttRows", () => {
  it("start = 帧 id 时刻 − durationMs;并发工具条形重叠", () => {
    const m = buildGanttRows(fx.concurrentTools);
    const [a, b] = m.rows.filter((r) => r.kind === "tool");
    expect(a.startMs + (a.durationMs ?? 0)).toBe(b.startMs + (b.durationMs ?? 0));
    expect(Math.abs(a.startMs - b.startMs)).toBe(700);
  });

  it("id 缺失 → 退化顺序拼接且 degraded=true", () => {
    const m = buildGanttRows(fx.noIds);
    expect(m.degraded).toBe(true);
    const [r0, r1] = m.rows;
    expect(r1.startMs).toBe(r0.startMs + (r0.durationMs ?? 0));
  });

  it("worker 行挂在 sub_agent 工具行下,depth=2,detail 指向所属步", () => {
    const m = buildGanttRows(fx.delegation);
    const worker = m.rows.find((r) => r.kind === "worker");
    expect(worker).toBeDefined();
    expect(worker?.depth).toBe(2);
    expect(worker?.detail.type).toBe("parentStep");
    expect(worker?.detail.type === "parentStep" && worker.detail.item.kind).toBe("agent");
    expect(
      worker?.detail.type === "parentStep" &&
        worker.detail.item.kind === "agent" &&
        worker.detail.item.stepCount,
    ).toBe(1);
    // Not "degraded" — every frame in this fixture carries a valid id.
    expect(m.degraded).toBe(false);
  });

  it("settled turn 的末条 agent 行 kind=final;running 中最新无 duration 行 durationMs=null", () => {
    const settled = buildGanttRows(fx.settledRun, { settled: true });
    const agentLike = settled.rows.filter((r) => r.kind === "agent" || r.kind === "final");
    expect(agentLike).toHaveLength(2);
    expect(agentLike[0].kind).toBe("agent");
    expect(agentLike[1].kind).toBe("final");

    const running = buildGanttRows(fx.runningRun);
    const runningAgentRows = running.rows.filter((r) => r.kind === "agent" || r.kind === "final");
    expect(runningAgentRows).toHaveLength(2);
    expect(runningAgentRows[1].kind).toBe("agent"); // not settled → no "final" relabel
    expect(runningAgentRows[1].durationMs).toBeNull();
  });

  it("marker(retry/error/end)不占行,进 markers[]", () => {
    const m = buildGanttRows(fx.withMarkers);
    expect(m.rows).toHaveLength(1); // only the one agent step — markers never become rows
    expect(m.markers.map((mk) => mk.kind)).toEqual(["retry", "error", "end"]);
  });
});
