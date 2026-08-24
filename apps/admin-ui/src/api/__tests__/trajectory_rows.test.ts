import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { compactRowsOf, ledgerRowsOf, promptInputsOf, resolveGanttKey, type AssistantRow } from "../trajectory_rows";

function ev(event: string, data: unknown, at = "t"): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: at };
}
function upd(node: string, channels: Record<string, unknown>, at = "t"): SseEvent {
  return ev("updates", { [node]: channels }, at);
}
// Real SSE frame id (`"{server_ms}-{seq}"`, see sse_id.ts) — the `ev`/`upd`
// helpers above hardcode `id: null`, which makes `serverMsOf` return `null`
// for every row; this variant is for the serverMs-sourcing test only.
function wire(event: string, data: unknown, id: string): SseEvent {
  return { id, event, data, rawData: "", receivedAt: "t" };
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

  it("tool statuses map: no result yet→running, error→error; approval marker → pause row; a failed tool flips its step's think row to error", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "",
        additional_kwargs: { reasoning_content: "先查 a 再查 b" },
        tool_calls: [
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
    const think = rows.find((r) => r.kind === "think");
    expect(think).toMatchObject({ status: "error" });
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

describe("resolveGanttKey", () => {
  it("maps item-/tool-/worker- keys to row ids (planner merged into update_plan resolves through plannerSeq)", () => {
    const rows = ledgerRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "p1", name: "update_plan", args: { goal: "g", steps: PLAN.steps } }] }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }], plan: PLAN }),
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }] }),
    ], INPUT);
    // Task 11 起只剩账本投影:agent 步是 `assistant:<seq>` 行(旧 think 行退役),
    // `item-<seq>` 照旧要落到同一步上。
    const assistant = rows.find((r) => r.kind === "assistant");
    const plan = rows.find((r) => r.kind === "plan");
    const memory = rows.find((r) => r.kind === "memory");
    expect(resolveGanttKey(rows, `item-${assistant!.seq}`)).toBe(assistant!.id);
    expect(resolveGanttKey(rows, "tool-p1")).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${(plan as { plannerSeq: number }).plannerSeq}`)).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${memory!.seq}`)).toBe(memory!.id);
    expect(resolveGanttKey(rows, "tool-nope")).toBeNull();

    // worker: 沿用 subagent 那条测试的 fixture,断言 `worker-<workerId>-0` → 那条 subagent 行 id
    const workerRows = ledgerRowsOf(workerFixtureEvents(), INPUT);
    const sub = workerRows.find((r) => r.kind === "subagent");
    expect(resolveGanttKey(workerRows, "worker-w-9-0")).toBe(sub!.id);
  });
});

describe("ledgerRowsOf", () => {
  const LEDGER_EVENTS: SseEvent[] = [
    upd("agent", { step_count: 1, _duration_ms: 900, messages: [{
      type: "ai", content: "先看一眼档案",
      response_metadata: { model_name: "gpt-x", finish_reason: "tool_calls" },
      usage_metadata: {
        input_tokens: 120, output_tokens: 30,
        output_token_details: { reasoning: 12 }, input_token_details: { cache_read: 64 },
      },
      additional_kwargs: { reasoning_content: "先查客户档案\n再看工单" },
      tool_calls: [
        { id: "c1", name: "query_crm", args: { id: "C-1" } },
        { id: "p1", name: "update_plan", args: { goal: "出建议", steps: PLAN.steps } },
      ],
    }] }),
    upd("tools", {
      messages: [
        { type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" },
        { type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" },
      ],
      plan: PLAN,
    }),
    upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "老客户", importance: 0.5, confidence: 0.5 }] }),
    upd("reflect", { reflections: [{ verdict: "pass", critique: "够了" }] }),
    ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }),
    upd("agent", { step_count: 2, _duration_ms: 400, messages: [{
      type: "ai", content: "最终答案", response_metadata: { model_name: "gpt-x", finish_reason: "stop" },
      usage_metadata: { input_tokens: 200, output_tokens: 40 },
    }] }),
    ev("end", { status: "success" }),
  ];

  it("ledgerRowsOf: user first, then one assistant per agent step (id assistant:<seq>, step, text=content, reasoning, tokens, finishReason, toolCallCount)", () => {
    const rows = ledgerRowsOf(LEDGER_EVENTS, INPUT);
    expect(rows[0]).toMatchObject({ id: "user", kind: "user", text: "帮我看看这个客户", seq: -1 });
    const assistants = rows.filter((r): r is AssistantRow => r.kind === "assistant");
    expect(assistants.map((r) => r.id)).toEqual(["assistant:0", "assistant:5"]);
    expect(assistants[0]).toMatchObject({
      seq: 0, step: 1, text: "先看一眼档案", reasoning: "先查客户档案\n再看工单",
      model: "gpt-x", inputTokens: 120, outputTokens: 30, reasoningTokens: 12, cacheReadTokens: 64,
      finishReason: "tool_calls", toolCallCount: 2, status: "ok", durationMs: 900, eventIndexes: [0],
    });
    expect(assistants[1]).toMatchObject({
      seq: 5, step: 2, text: "最终答案", reasoning: "", model: "gpt-x",
      inputTokens: 200, outputTokens: 40, finishReason: "stop", toolCallCount: 0, durationMs: 400,
    });
    expect(assistants[1].reasoningTokens).toBeUndefined();
    expect(assistants[1].cacheReadTokens).toBeUndefined();
  });

  it("ledgerRowsOf: no think rows and no trailing synthetic assistant", () => {
    const rows = ledgerRowsOf(LEDGER_EVENTS, INPUT);
    expect(rows.some((r) => r.kind === "think")).toBe(false);
    // 末行是最后一个 agent 步的 assistant(该步的 item.seq 是 5),不是旧的 `id: "assistant"` 合成行。
    expect(rows.at(-1)).toMatchObject({ id: "assistant:5" });
    expect(rows.some((r) => r.id === "assistant")).toBe(false);
  });

  it("ledgerRowsOf: tools / plan / subagent / memory / reflect / marker rows keep the same ids as compactRowsOf", () => {
    const withWorker = [...LEDGER_EVENTS, ...workerFixtureEvents()];
    const ledger = ledgerRowsOf(withWorker, INPUT);
    const compact = compactRowsOf(withWorker);
    const nonAgent = (id: string): boolean => !id.startsWith("think:") && !id.startsWith("assistant:");
    const compactIds = compact.map((r) => r.id).filter(nonAgent);
    const ledgerIds = ledger.map((r) => r.id).filter(nonAgent).filter((id) => id !== "user");
    expect(compactIds).toEqual(ledgerIds);
    expect(compactIds).toEqual([
      "tool:0:0", "plan:0:1", "memory:2", "reflect:3", "retry:4", "tool:7:0", "subagent:7:0:0",
    ]);
  });

  it("ledgerRowsOf: a step with no content yields text '' (caller renders tool-call-only)", () => {
    const rows = ledgerRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
    ], INPUT);
    const assistant = rows.find((r): r is AssistantRow => r.kind === "assistant");
    expect(assistant).toMatchObject({ id: "assistant:0", text: "", reasoning: "", toolCallCount: 1 });
  });

  it("ledgerRowsOf: step error → assistant status error", () => {
    const rows = ledgerRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "试试", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "a", name: "t1", content: "boom", status: "error" }] }),
    ], INPUT);
    const assistant = rows.find((r): r is AssistantRow => r.kind === "assistant");
    expect(assistant).toMatchObject({ status: "error" });
    expect(rows.find((r) => r.kind === "tool")).toMatchObject({ status: "error" });
  });

  // 迁自退役的 `describe("trajectoryRowsOf")` —— serverMs 的取值规则是
  // `rowsOf` 共有的,只是承载它的投影换成了账本。
  it("serverMs: item.serverMs for assistant/marker rows, entry.serverMs for tool rows, null for user/subagent", () => {
    const rows = ledgerRowsOf([
      wire("updates", { agent: { step_count: 1, messages: [{
        type: "ai", content: "", tool_calls: [{ id: "call-9", name: "spawn_worker", args: { task: "x" } }],
      }] } }, "1700000000000-1"),
      wire("worker", {
        worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
        label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "start", wseq: 0,
        data: { task_excerpt: "x", role: null, max_steps: 8 },
      }, "1700000000100-2"),
      wire("updates", { tools: { messages: [
        { type: "tool", tool_call_id: "call-9", name: "spawn_worker", content: "ok", status: "success" },
      ] } }, "1700000000400-3"),
      wire("worker", {
        worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
        label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "end", wseq: 1,
        data: { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 42 },
      }, "1700000000500-4"),
      wire("approval", { tool_call_id: "call-9" }, "1700000000900-5"),
    ], INPUT);
    const user = rows.find((r) => r.kind === "user");
    const assistant = rows.find((r) => r.kind === "assistant");
    const tool = rows.find((r) => r.kind === "tool");
    const subagent = rows.find((r) => r.kind === "subagent");
    const approval = rows.find((r) => r.kind === "approval");
    // assistant/marker rows take the AgentStep/MarkerItem's own frame's ms.
    expect(assistant?.serverMs).toBe(1700000000000);
    expect(approval?.serverMs).toBe(1700000000900);
    // the tool row takes the RESULT frame's ms via entry.serverMs, not the
    // CALL frame's ms (item.serverMs would be 1700000000000, the wrong one).
    expect(tool?.serverMs).toBe(1700000000400);
    // no serverMs concept for these kinds.
    expect(user?.serverMs).toBeNull();
    expect(subagent?.serverMs).toBeNull();
  });
});

describe("ledgerRowsOf — SYSTEM row (PR-A.3 §十.1)", () => {
  const input = { text: "hi", attachmentNames: [], inputs: {} };
  it("prepends a system row when the run carries a system_prompt frame", () => {
    // 帧 id 须满足 serverMsOf 的 `\d{10,}-\d+` 格式(真实 SSE id 是 epoch 毫秒,
    // 至少 10 位)才能被解析出 serverMs —— 用 10 位定长 id,而非仅作示意的短数字。
    const events: SseEvent[] = [
      wire("metadata", { run_id: "r", thread_id: "t" }, "1000000000-1"),
      wire("system_prompt", { text: "你是评审员\n第二行" }, "1000000001-2"),
      wire("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "ok", usage_metadata: {} }] } }, "1000000002-3"),
    ];
    const rows = ledgerRowsOf(events, input);
    expect(rows.map((r) => r.kind)).toEqual(["system", "user", "assistant"]);
    expect(rows[0]).toMatchObject({ id: "system", kind: "system", text: "你是评审员\n第二行", seq: -1, eventIndexes: [1], serverMs: 1000000001 });
  });
  it("no frame / empty text → no system row; compact projection never sees it", () => {
    const events: SseEvent[] = [wire("system_prompt", { text: "" }, "1000000001-2")];
    expect(ledgerRowsOf(events, input).map((r) => r.kind)).toEqual(["user"]);
    expect(ledgerRowsOf([], input).map((r) => r.kind)).toEqual(["user"]);
    expect(compactRowsOf([wire("system_prompt", { text: "x" }, "1000000001-2")]).map((r) => r.kind)).not.toContain("system");
  });
  it("assistant row carries firstTokenMs from the step", () => {
    const events: SseEvent[] = [
      wire("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "ok", additional_kwargs: { first_token_ms: 640 }, usage_metadata: {} }] } }, "1000000002-3"),
    ];
    const assistant = ledgerRowsOf(events, input).find((r) => r.kind === "assistant");
    expect(assistant).toMatchObject({ kind: "assistant", firstTokenMs: 640 });
  });
});


describe("promptInputsOf (BUG-16)", () => {
  it("accepts a flat string map and rejects everything else", () => {
    expect(promptInputsOf({ text: "p", inputs: { a: "1" } })).toEqual({ a: "1" });
    expect(promptInputsOf({ text: "p" })).toBeNull();
    expect(promptInputsOf({ inputs: {} })).toBeNull();
    expect(promptInputsOf({ inputs: { a: 1 } })).toBeNull();
    expect(promptInputsOf({ inputs: ["a"] })).toBeNull();
    expect(promptInputsOf(null)).toBeNull();
  });
});
