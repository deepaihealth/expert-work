import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { parseWorkerFrames } from "../worker_timeline";

let n = 0;
function wf(kind: string, over: Record<string, unknown> = {}, data: Record<string, unknown> = {}): SseEvent {
  n += 1;
  return {
    id: String(n),
    event: "worker",
    data: {
      worker_id: "w-1",
      parent_worker_id: null,
      parent_tool_call_id: "call-1",
      label: "spawn_worker",
      agent_ref: "dynamic:research",
      depth: 1,
      kind,
      wseq: n,
      data,
      ...over,
    },
    rawData: "",
    receivedAt: new Date().toISOString(),
  };
}

describe("parseWorkerFrames", () => {
  it("folds start/update/end into one WorkerTimeline", () => {
    const events = [
      wf("start", { wseq: 0 }, { task_excerpt: "research X", role: "research", max_steps: 32 }),
      wf("update", { wseq: 1 }, {
        node: "agent", step_count: 1, _duration_ms: 120,
        messages: [{ type: "ai", content_excerpt: "thinking", tool_calls: [{ name: "http_request", args_excerpt: "{}" }] }],
      }),
      wf("update", { wseq: 2 }, {
        node: "tools", _duration_ms: 300,
        messages: [{ type: "tool", name: "http_request", tool_result_excerpt: "<html>" }],
      }),
      wf("end", { wseq: 3 }, { outcome: "success", iteration_used: 2, llm_call_count: 1, wall_clock_ms: 900 }),
    ];
    const map = parseWorkerFrames(events);
    const [w] = map.get("call-1") ?? [];
    expect(w.workerId).toBe("w-1");
    expect(w.role).toBe("research");
    expect(w.taskExcerpt).toBe("research X");
    expect(w.maxSteps).toBe(32);
    expect(w.status).toBe("success");
    expect(w.steps).toHaveLength(2);
    expect(w.steps[0].node).toBe("agent");
    expect(w.steps[0].stepCount).toBe(1);
    expect(w.steps[0].durationMs).toBe(120);
    expect(w.steps[0].messages[0].toolCalls?.[0].name).toBe("http_request");
    expect(w.steps[1].messages[0].toolResultExcerpt).toBe("<html>");
    expect(w.summary).toEqual({ iterationUsed: 2, llmCallCount: 1, wallClockMs: 900 });
  });

  it("no end frame → status running, summary null", () => {
    const map = parseWorkerFrames([
      wf("start", { wseq: 0 }, { task_excerpt: "t", role: null, max_steps: 8 }),
      wf("update", { wseq: 1 }, { node: "agent", _duration_ms: 5, messages: [] }),
    ]);
    const [w] = map.get("call-1") ?? [];
    expect(w.status).toBe("running");
    expect(w.summary).toBeNull();
  });

  it("nests grandchild under parent via parent_worker_id", () => {
    const map = parseWorkerFrames([
      wf("start", { wseq: 0 }, { task_excerpt: "p", role: null, max_steps: 8 }),
      wf("start", { worker_id: "w-2", parent_worker_id: "w-1", parent_tool_call_id: "inner-call", depth: 2, wseq: 0 },
        { task_excerpt: "c", role: null, max_steps: 8 }),
      wf("end", { worker_id: "w-2", parent_worker_id: "w-1", parent_tool_call_id: "inner-call", depth: 2, wseq: 1 },
        { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 10 }),
      wf("end", { wseq: 1 }, { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 99 }),
    ]);
    const roots = map.get("call-1") ?? [];
    expect(roots).toHaveLength(1);
    expect(roots[0].children).toHaveLength(1);
    expect(roots[0].children[0].workerId).toBe("w-2");
    // 孙 worker 不出现在顶层 map(inner-call 不是父 run 的工具卡)
    expect(map.has("inner-call")).toBe(false);
  });

  it("end without start still yields an entry (degraded)", () => {
    const map = parseWorkerFrames([
      wf("end", { wseq: 0 }, { outcome: "cancelled", iteration_used: 0, llm_call_count: 0, wall_clock_ms: 1 }),
    ]);
    const [w] = map.get("call-1") ?? [];
    expect(w.status).toBe("cancelled");
    expect(w.taskExcerpt).toBe("");
  });

  it("drops frames without parent_tool_call_id at depth-1 and malformed data without throwing", () => {
    const map = parseWorkerFrames([
      wf("start", { parent_tool_call_id: null, wseq: 0 }, { task_excerpt: "orphan", role: null, max_steps: 1 }),
      { id: "x", event: "worker", data: "not-an-object", rawData: "", receivedAt: "" } as SseEvent,
      { id: "y", event: "updates", data: {}, rawData: "", receivedAt: "" } as SseEvent,
    ]);
    expect(map.size).toBe(0);
  });

  it("lifts the exec artifact summary off a worker tool message", () => {
    const map = parseWorkerFrames([
      wf("update", { wseq: 1 }, {
        node: "tools",
        _duration_ms: 5,
        messages: [{
          type: "tool",
          name: "exec_python",
          tool_result_excerpt: "stdout:▁ 1▁ exit_code:▁ 0",
          exec: { exit_code: 0, timed_out: false, stdout_excerpt: "1\n", stderr_excerpt: "" },
        }],
      }),
    ]);
    const [w] = map.get("call-1") ?? [];
    expect(w.steps[0].messages[0].exec).toEqual({
      exitCode: 0,
      timedOut: false,
      stdoutExcerpt: "1\n",
      stderrExcerpt: "",
    });
  });

  it("multiple workers on one tool call keep arrival order", () => {
    const map = parseWorkerFrames([
      wf("start", { worker_id: "a", wseq: 0 }, { task_excerpt: "1", role: null, max_steps: 1 }),
      wf("start", { worker_id: "b", wseq: 0 }, { task_excerpt: "2", role: null, max_steps: 1 }),
    ]);
    expect((map.get("call-1") ?? []).map((w) => w.workerId)).toEqual(["a", "b"]);
  });
});
