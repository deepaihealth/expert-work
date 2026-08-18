import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { compactRowsOf, resolveGanttKey, trajectoryRowsOf } from "../trajectory_rows";

function ev(event: string, data: unknown, at = "t"): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: at };
}
function upd(node: string, channels: Record<string, unknown>, at = "t"): SseEvent {
  return ev("updates", { [node]: channels }, at);
}
const PLAN = { goal: "出建议", steps: [
  { id: "1", description: "查档案", status: "completed" },
  { id: "2", description: "分析", status: "in_progress" },
  { id: "3", description: "出建议", status: "pending" },
] };
const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

// 复用 worker_timeline.test.ts 里 spawn_worker 的最小 fixture:一次工具调用
// (call-9)派生一个 worker(w-9)的 start/end 帧。
function workerFixtureEvents(): SseEvent[] {
  return [
    upd("agent", { step_count: 1, messages: [{
      type: "ai", content: "", tool_calls: [{ id: "call-9", name: "spawn_worker", args: { task: "x" } }],
    }] }),
    ev("worker", {
      worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
      label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "start", wseq: 0,
      data: { task_excerpt: "x", role: null, max_steps: 8 },
    }),
    ev("worker", {
      worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
      label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "end", wseq: 1,
      data: { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 42 },
    }),
  ];
}

describe("compactRowsOf", () => {
  it("agent step → think row then one row per tool, in order; think carries model/tokens", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, _duration_ms: 900, messages: [{
        type: "ai", content: "",
        response_metadata: { model_name: "gpt-x" }, usage_metadata: { input_tokens: 120, output_tokens: 30 },
        additional_kwargs: { reasoning_content: "先查客户档案\n再看工单" },
        tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
      }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }] }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["think", "tool"]);
    expect(rows[0]).toMatchObject({ kind: "think", step: 1, text: "先查客户档案\n再看工单", status: "ok", model: "gpt-x", inputTokens: 120, outputTokens: 30, durationMs: 900, eventIndexes: [0] });
    expect(rows[1]).toMatchObject({ kind: "tool", step: 1, status: "ok", eventIndexes: [0, 1] });
    if (rows[1].kind === "tool") {
      expect(rows[1].entry.toolName).toBe("query_crm");
      expect(rows[1].entry.resultPreview).toContain("3 条记录");
    }
  });

  it("a step without reasoning has NO think row in the compact projection", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["tool"]);
  });

  it("tool statuses map: no result yet→running, error→error; approval marker → pause row", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "a", name: "t1", args: {} }, { id: "b", name: "t2", args: {} },
      ] }] }),
      upd("tools", { messages: [
        { type: "tool", tool_call_id: "b", name: "t2", content: "boom", status: "error" },
      ] }),
      ev("approval", { tool_call_id: "a" }),
    ]);
    const tools = rows.filter((r) => r.kind === "tool");
    expect(tools.map((r) => r.status)).toEqual(["running", "error"]);
    expect(rows.at(-1)).toMatchObject({ kind: "approval", status: "pause", text: "等待人工审批", eventIndexes: [2] });
  });

  it("update_plan call + the tools node's plan snapshot merge into ONE plan row (callId / plannerSeq / both frames)", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "p1", name: "update_plan", args: { goal: "出建议", steps: PLAN.steps, reason: "档案查完了,细化后两步" } },
      ] }] }),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }],
        plan: PLAN,
      }),
    ]);
    const plans = rows.filter((r) => r.kind === "plan");
    expect(plans).toHaveLength(1);
    expect(plans[0]).toMatchObject({ kind: "plan", source: "update_plan", callId: "p1", plannerSeq: 1, stepsTotal: 3, goal: "出建议", reason: "档案查完了,细化后两步", plan: PLAN, step: 2, eventIndexes: [0, 1] });
  });

  it("two update_plan calls in one batch: snapshot merges into the LAST one, the earlier keeps args-only", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "p1", name: "update_plan", args: { goal: "g", steps: [{ id: "1", description: "a", status: "pending" }] } },
        { id: "p2", name: "update_plan", args: { goal: "g", steps: PLAN.steps } },
      ] }] }),
      upd("tools", { messages: [
        { type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" },
        { type: "tool", tool_call_id: "p2", name: "update_plan", content: "ok", status: "success" },
      ], plan: PLAN }),
    ]);
    const plans = rows.filter((r) => r.kind === "plan");
    expect(plans).toHaveLength(2);
    expect(plans[0]).toMatchObject({ stepsTotal: 1, plan: null, plannerSeq: null });
    expect(plans[1]).toMatchObject({ stepsTotal: 3, plan: PLAN });
  });

  it("planner node's plan (no preceding update_plan) is its own 'planner' row", () => {
    const rows = compactRowsOf([upd("planner", { plan: PLAN, _duration_ms: 1200 })]);
    expect(rows).toEqual([expect.objectContaining({ kind: "plan", source: "planner", callId: null, stepsTotal: 3, goal: "出建议", plan: PLAN, durationMs: 1200, step: null, eventIndexes: [0] })]);
  });

  it("aux + marker rows: memory recall/writeback counts (detail.memories), reflect verdict, compaction/retry/error texts; end is dropped", () => {
    const rows = compactRowsOf([
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }, { id: "m2", kind: "fact", content: "y", importance: 0.5, confidence: 0.5 }] }),
      upd("reflect", { reflections: [{ verdict: "revise", critique: "漏了夜间" }] }),
      upd("memory_writeback", { written_memories: [{ id: "w1" }] }),
      ev("compaction", { passes: 1, tokens_before: 12300, tokens_after: 4100, summary_chars: 800 }),
      ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }),
      ev("error", { message: "上游 502" }),
      ev("end", { status: "error" }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["memory", "reflect", "memory", "compaction", "retry", "error"]);
    expect(rows[0]).toMatchObject({ direction: "recall", count: 2 });
    expect(rows[1]).toMatchObject({ verdict: "revise", status: "warn" });
    expect(rows[2]).toMatchObject({ direction: "writeback", count: 1 });
    expect(rows[3]).toMatchObject({ status: "warn", eventIndexes: [3] });
    expect(rows[5]).toMatchObject({ kind: "error", text: "上游 502", status: "error" });
  });

  it("a tool with worker sub-timelines gets one subagent row per worker right after it, carrying the worker frames' indexes", () => {
    const events = workerFixtureEvents();
    const rows = compactRowsOf(events);
    expect(rows.map((r) => r.kind)).toEqual(["tool", "subagent"]);
    const tool = rows[0];
    const sub = rows[1];
    if (tool.kind === "tool" && sub.kind === "subagent") {
      expect(sub.parentEntryId).toBe(tool.entry.id);
      expect(sub.worker.workerId).toBe("w-9");
      expect(sub.status).toBe("ok");
      expect(sub.eventIndexes).toEqual([1, 2]);
    } else {
      throw new Error("expected [tool, subagent]");
    }
  });

  it("row ids are unique within a turn", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r" }, tool_calls: [{ id: "a", name: "t", args: {} }, { id: "b", name: "t", args: {} }] }] }),
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r2" } }] }),
    ]);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
  });
});

describe("trajectoryRowsOf", () => {
  const EVENTS = [
    upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
    upd("tools", { messages: [{ type: "tool", tool_call_id: "a", name: "t1", content: "r", status: "success" }] }),
    upd("agent", { step_count: 2, _duration_ms: 400, messages: [{ type: "ai", content: "最终答案", response_metadata: { model_name: "gpt-x" } }] }),
    ev("end", { status: "success" }),
  ];
  it("user first, one think per agent step even without reasoning, assistant last; ids shared with the compact projection", () => {
    const rows = trajectoryRowsOf(EVENTS, INPUT, "最终答案", "done");
    expect(rows.map((r) => r.kind)).toEqual(["user", "think", "tool", "think", "assistant"]);
    expect(rows[0]).toMatchObject({ id: "user", text: "帮我看看这个客户", seq: -1 });
    expect(rows[1]).toMatchObject({ kind: "think", text: "", step: 1 });
    expect(rows[3]).toMatchObject({ kind: "think", text: "", step: 2, model: "gpt-x", durationMs: 400 });
    expect(rows.at(-1)).toMatchObject({ id: "assistant", kind: "assistant", text: "最终答案", status: "ok", eventIndexes: [2] });
    const compactIds = new Set(compactRowsOf(EVENTS).map((r) => r.id));
    for (const id of compactIds) expect(rows.some((r) => r.id === id)).toBe(true);
  });
  it("assistant row omitted while answer is null; status follows turnStatus", () => {
    expect(trajectoryRowsOf(EVENTS, INPUT, null, "running").some((r) => r.kind === "assistant")).toBe(false);
    expect(trajectoryRowsOf(EVENTS, INPUT, "x", "error").at(-1)).toMatchObject({ kind: "assistant", status: "error" });
  });
});

describe("resolveGanttKey", () => {
  it("maps item-/tool-/worker- keys to row ids (planner merged into update_plan resolves through plannerSeq)", () => {
    const rows = trajectoryRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "p1", name: "update_plan", args: { goal: "g", steps: PLAN.steps } }] }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }], plan: PLAN }),
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }] }),
    ], INPUT, null, "running");
    const think = rows.find((r) => r.kind === "think");
    const plan = rows.find((r) => r.kind === "plan");
    const memory = rows.find((r) => r.kind === "memory");
    expect(resolveGanttKey(rows, `item-${think!.seq}`)).toBe(think!.id);
    expect(resolveGanttKey(rows, "tool-p1")).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${(plan as { plannerSeq: number }).plannerSeq}`)).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${memory!.seq}`)).toBe(memory!.id);
    expect(resolveGanttKey(rows, "tool-nope")).toBeNull();

    // worker: 沿用 subagent 那条测试的 fixture,断言 `worker-<workerId>-0` → 那条 subagent 行 id
    const workerRows = trajectoryRowsOf(workerFixtureEvents(), INPUT, null, "running");
    const sub = workerRows.find((r) => r.kind === "subagent");
    expect(resolveGanttKey(workerRows, "worker-w-9-0")).toBe(sub!.id);
  });
});
