/**
 * 账本构建器测试(PR-A.2 Task 2)—— `buildLedger` / `absoluteSpans` /
 * `ledgerRecordId` / `lastKnownFrame`。fixture:两轮 history done + 一轮
 * live running,`ConsoleTurn` 手工造。
 */
import { describe, expect, it } from "vitest";

import type { SseEvent } from "../../../api/sessions";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";
import { absoluteSpans, buildLedger, lastKnownFrame, ledgerRecordId } from "../ledger";
import { ledgerRowsOf } from "../../../api/trajectory_rows";
import type { ConsoleTurn } from "../types";

// serverMsOf 要求 10+ 位的 ms 段(真 epoch ms),沿用 lane_strip_model.test.ts
// 的约定:小偏移量加在一个真实 epoch 基准上。
const BASE = 1_700_000_000_000;
let seqCounter = 0;

function ev(event: string, data: unknown, ms: number | null): SseEvent {
  seqCounter += 1;
  return { id: ms === null ? null : `${BASE + ms}-${seqCounter}`, event, data, rawData: "", receivedAt: "2026-01-01T00:00:00.000Z" };
}
function upd(node: string, channels: Record<string, unknown>, ms: number | null): SseEvent {
  return ev("updates", { [node]: channels }, ms);
}
function call(id: string, name: string, args: Record<string, unknown> = {}) {
  return { id, name, args };
}
function result(id: string, name: string, durationMs: number, content = "ok", status: "success" | "error" = "success") {
  return { type: "tool", tool_call_id: id, name, content, status, additional_kwargs: { duration_ms: durationMs } };
}
const PLAN = { goal: "出建议", steps: [{ id: "1", description: "查档案", status: "completed" }] };

function turnOf(over: Partial<ConsoleTurn> & Pick<ConsoleTurn, "key" | "seq">): ConsoleTurn {
  return {
    source: "history",
    turn: { id: over.key, input: "问题", attachments: [], inputs: {}, events: [], status: "done", error: null, approval: null },
    runId: `run-${over.seq}`,
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
    createdAt: null,
    ...over,
  };
}

/** 轮 A(history · done):step1(工具 query_crm)+ step2(终答)。 */
function turnAEvents(): SseEvent[] {
  return [
    upd("agent", { step_count: 1, _duration_ms: 400, messages: [{
      type: "ai", content: "",
      additional_kwargs: { reasoning_content: "先查客户档案" },
      usage_metadata: { input_tokens: 100, output_tokens: 20 },
      tool_calls: [call("c1", "query_crm", { id: "C-1" })],
    }] }, 1000),
    upd("tools", { messages: [result("c1", "query_crm", 300, "3 条记录\n第二行")] }, 1400),
    upd("agent", { step_count: 2, _duration_ms: 500, messages: [{
      type: "ai", content: "结论",
      response_metadata: { model_name: "gpt-x", finish_reason: "stop" },
      usage_metadata: { input_tokens: 200, output_tokens: 40 },
    }] }, 2300),
  ];
}
/** 轮 C(live · running):step1 已落帧、工具还挂着;step2 只在 live 缓冲里。 */
function turnCEvents(): SseEvent[] {
  return [
    upd("agent", { step_count: 1, _duration_ms: 300, messages: [{
      type: "ai", content: "先搜一下",
      usage_metadata: { input_tokens: 50, output_tokens: 10 },
      tool_calls: [call("c9", "search")],
    }] }, 5000),
  ];
}
const LIVE_BY_STEP: ReadonlyMap<number, LiveStep> = new Map<number, LiveStep>([
  [2, { content: "正在写", reasoning: "想想", toolNames: new Map([[0, "calc"]]), reasoningMs: null }],
]);
const CREATED_B = new Date(BASE + 10_000).toISOString();

function fixture(): ConsoleTurn[] {
  return [
    turnOf({ key: "A", seq: 0, turn: { id: "A", input: "问题 A", attachments: [], inputs: {}, events: turnAEvents(), status: "done", error: null, approval: null } }),
    turnOf({ key: "B", seq: 1, loadState: "pending", createdAt: CREATED_B,
      fallbackLines: [{ text: "历史回答\n第二行", channel: "final" }],
      turn: { id: "B", input: "问题 B\n补充", attachments: [], inputs: {}, events: [], status: "done", error: null, approval: null } }),
    turnOf({ key: "C", seq: 2, source: "live", runId: "run-2",
      turn: { id: "C", input: "问题 C", attachments: [], inputs: {}, events: turnCEvents(), status: "running", error: null, approval: null } }),
  ];
}
const NOW = BASE + 9_000;
function build(): ReturnType<typeof buildLedger> {
  return buildLedger({ turns: fixture(), streamTurnKey: "C", liveByStep: LIVE_BY_STEP, nowMs: NOW });
}

describe("buildLedger", () => {
  it("records are in turn order with turnStart/turnEnd and 0-based index", () => {
    const ledger = build();
    expect(ledger.records.map((r) => r.id)).toEqual([
      "A/user", "A/assistant:0", "A/tool:0:0", "A/assistant:1",
      "B/user", "B/assistant:placeholder",
      "C/user", "C/assistant:0", "C/tool:0:0", "C/live-assistant:2", "C/live-tool:2:0",
    ]);
    expect(ledger.records.map((r) => r.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(ledger.records.filter((r) => r.turnStart).map((r) => r.id)).toEqual(["A/user", "B/user", "C/user"]);
    expect(ledger.records.filter((r) => r.turnEnd).map((r) => r.id)).toEqual([
      "A/assistant:1", "B/assistant:placeholder", "C/live-tool:2:0",
    ]);
    expect(ledger.records.map((r) => r.turnSeq)).toEqual([0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 2]);
    expect(ledger.records[0]).toMatchObject({ turnKey: "A", runId: "run-0", kind: "user", placeholder: null });
    expect(ledger.records[2].events).toHaveLength(3);
  });

  it("turns[] carries one entry per turn with firstIndex/lastIndex/status/requestNos", () => {
    const ledger = build();
    expect(ledger.turns).toEqual([
      { key: "A", seq: 0, runId: "run-0", status: "done", firstIndex: 0, lastIndex: 3, requestNos: [1, 2] },
      { key: "B", seq: 1, runId: "run-1", status: "done", firstIndex: 4, lastIndex: 5, requestNos: [] },
      { key: "C", seq: 2, runId: "run-2", status: "running", firstIndex: 6, lastIndex: 10, requestNos: [3, 4] },
    ]);
  });

  it("assistant records get requestNo 1..n across turns; tools/plans/subagents get ownerRequestNo and parentId", () => {
    const byId = new Map(build().records.map((r) => [r.id, r]));
    expect(byId.get("A/assistant:0")).toMatchObject({ requestNo: 1, ownerRequestNo: 1, parentId: null });
    expect(byId.get("A/assistant:1")).toMatchObject({ requestNo: 2, ownerRequestNo: 2 });
    expect(byId.get("A/tool:0:0")).toMatchObject({ requestNo: null, ownerRequestNo: 1, parentId: "A/assistant:0" });
    expect(byId.get("A/user")).toMatchObject({ requestNo: null, ownerRequestNo: null, parentId: null });
    // 占位 assistant 不开请求。
    expect(byId.get("B/assistant:placeholder")).toMatchObject({ requestNo: null, ownerRequestNo: null });
    expect(byId.get("C/assistant:0")).toMatchObject({ requestNo: 3 });
    // live 行按 step 认领主人(seq 都是 -1,认 seq 会串)。
    expect(byId.get("C/live-assistant:2")).toMatchObject({ requestNo: 4 });
    expect(byId.get("C/live-tool:2:0")).toMatchObject({ ownerRequestNo: 4, parentId: "C/live-assistant:2" });
    expect(byId.get("C/tool:0:0")).toMatchObject({ ownerRequestNo: 3, parentId: "C/assistant:0" });
  });

  it("subagent records hang off their parent tool record, not the assistant", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 100, messages: [{ type: "ai", content: "", tool_calls: [call("call-9", "spawn_worker", { task: "x" })] }] }, 100),
      ev("worker", { worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9", label: "调研员", agent_ref: "dynamic:general", depth: 1, kind: "start", wseq: 0, data: { task_excerpt: "查一下夜间排班", role: null, max_steps: 8 } }, 150),
      ev("worker", { worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9", label: "调研员", agent_ref: "dynamic:general", depth: 1, kind: "end", wseq: 1, data: { outcome: "success", iteration_used: 1, llm_call_count: 3, wall_clock_ms: 42 } }, 200),
      upd("tools", { messages: [result("call-9", "spawn_worker", 100)] }, 300),
    ];
    const turns = [turnOf({ key: "S", seq: 0, turn: { id: "S", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const byId = new Map(buildLedger({ turns, streamTurnKey: null, nowMs: NOW }).records.map((r) => [r.id, r]));
    expect(byId.get("S/subagent:0:0:0")).toMatchObject({
      ownerRequestNo: 1, parentId: "S/tool:0:0", lane: 2,
      text: "调研员 查一下夜间排班", resultText: "3 次模型调用 · 42ms",
    });
  });

  it("requests carry usage, cumulative sums, toolCalls and status", () => {
    const ledger = build();
    expect(ledger.requests.map((r) => r.no)).toEqual([1, 2, 3, 4]);
    expect(ledger.requests[0]).toMatchObject({
      no: 1, turnKey: "A", turnSeq: 0, step: 1, recordId: "A/assistant:0", status: "ok",
      usage: { input: 100, output: 20, reasoning: 0, cacheRead: 0 },
      cumulative: { input: 100, output: 20 }, toolCalls: 1,
      startedAt: BASE + 600, endedAt: BASE + 1000, durationMs: 400,
    });
    expect(ledger.requests[1]).toMatchObject({
      no: 2, model: "gpt-x", finishReason: "stop", toolCalls: 0,
      usage: { input: 200, output: 40, reasoning: 0, cacheRead: 0 },
      cumulative: { input: 300, output: 60 },
    });
    expect(ledger.requests[2]).toMatchObject({ no: 3, turnKey: "C", toolCalls: 1, cumulative: { input: 350, output: 70 } });
    expect(ledger.requests[3]).toMatchObject({ no: 4, status: "running", step: 2, toolCalls: 1, endedAt: NOW });
  });

  it("lane by kind: user/memory-recall → 0, assistant/reflect/compaction → 1, tool/subagent/plan/memory-writeback/markers → 2", () => {
    const events: SseEvent[] = [
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "老客户 A\n第二行", importance: 0.5, confidence: 0.5 }] }, 100),
      upd("agent", { step_count: 1, _duration_ms: 100, messages: [{ type: "ai", content: "做点事", tool_calls: [
        call("c1", "query_crm", { id: "C-1" }),
        call("p1", "update_plan", { goal: "出建议", steps: PLAN.steps, reason: "细化" }),
      ] }] }, 300),
      upd("tools", { messages: [result("c1", "query_crm", 50), result("p1", "update_plan", 10)], plan: PLAN }, 500),
      upd("reflect", { reflections: [{ verdict: "revise", critique: "漏了夜间\n补一下" }] }, 700),
      upd("memory_writeback", { written_memories: [{ id: "w1", content: "写回内容" }] }, 800),
      ev("compaction", { passes: 1, tokens_before: 100, tokens_after: 50 }, 900),
    ];
    const turns = [turnOf({ key: "L", seq: 0, turn: { id: "L", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const records = buildLedger({ turns, streamTurnKey: null, nowMs: NOW }).records;
    expect(records.map((r) => [r.kind, r.lane])).toEqual([
      ["user", 0], ["memory", 0], ["assistant", 1], ["tool", 2], ["plan", 2],
      ["reflect", 1], ["memory", 2], ["compaction", 1],
    ]);
    const byId = new Map(records.map((r) => [r.id, r]));
    expect(byId.get("L/memory:0")).toMatchObject({ text: "", resultText: "老客户 A" });
    expect(byId.get("L/reflect:3")).toMatchObject({ text: "revise", resultText: "漏了夜间" });
    expect(byId.get("L/compaction:5")?.text).toContain("压缩 1 遍");
    expect(byId.get("L/plan:1:1")).toMatchObject({
      text: `update_plan ${JSON.stringify({ goal: "出建议", steps: 1, reason: "细化" })}`,
      resultText: "1 步",
    });
    expect(byId.get("L/user")?.text).toBe("问");
  });

  it("tool text is `name argsJSON` (≤400 chars) and resultText the first result line; error rows are isError", () => {
    const bigArg = "x".repeat(600);
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 100, messages: [{ type: "ai", content: "", tool_calls: [
        call("c1", "query_crm", { q: bigArg }), call("c2", "boom"),
      ] }] }, 100),
      upd("tools", { messages: [
        result("c1", "query_crm", 50, "3 条记录\n第二行"),
        result("c2", "boom", 20, "炸了\n栈", "error"),
      ] }, 300),
    ];
    const turns = [turnOf({ key: "T", seq: 0, turn: { id: "T", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const byId = new Map(buildLedger({ turns, streamTurnKey: null, nowMs: NOW }).records.map((r) => [r.id, r]));
    const good = byId.get("T/tool:0:0");
    expect(good?.text.startsWith('query_crm {"q":"xxx')).toBe(true);
    expect(good?.text).toHaveLength(400);
    expect(good).toMatchObject({ resultText: "3 条记录", isError: false });
    expect(byId.get("T/tool:0:1")).toMatchObject({ text: "boom {}", resultText: "炸了", isError: true });
    // 步里有工具失败 → 该步的 assistant 记录也是失败。
    expect(byId.get("T/assistant:0")).toMatchObject({ isError: true });
  });

  it("startedAt/endedAt come from gantt absolute spans; user takes the turn's earliest start; timed=true when every record is timed", () => {
    const ledger = build();
    const byId = new Map(ledger.records.map((r) => [r.id, r]));
    expect(byId.get("A/assistant:0")).toMatchObject({ startedAt: BASE + 600, endedAt: BASE + 1000 });
    expect(byId.get("A/tool:0:0")).toMatchObject({ startedAt: BASE + 1100, endedAt: BASE + 1400 });
    expect(byId.get("A/assistant:1")).toMatchObject({ startedAt: BASE + 1800, endedAt: BASE + 2300 });
    expect(byId.get("A/user")).toMatchObject({ startedAt: BASE + 600, endedAt: BASE + 600 });
    // 未回放的轮:两条记录都钉在 createdAt。
    expect(byId.get("B/user")).toMatchObject({ startedAt: BASE + 10_000, endedAt: BASE + 10_000 });
    expect(ledger.timed).toBe(true);
  });

  it("a turn whose gantt is degraded → its records have null times and ledger.timed=false", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 100, messages: [{ type: "ai", content: "答" }] }, null),
    ];
    const turns = [turnOf({ key: "D", seq: 0, turn: { id: "D", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const ledger = buildLedger({ turns, streamTurnKey: null, nowMs: NOW });
    expect(ledger.records.map((r) => [r.startedAt, r.endedAt])).toEqual([[null, null], [null, null]]);
    expect(ledger.timed).toBe(false);
    expect(ledger.requests[0]).toMatchObject({ startedAt: null, endedAt: null });
  });

  it("history turn not yet replayed → USER + placeholder assistant (loading/error) at createdAt", () => {
    const ledger = build();
    const user = ledger.records[4];
    const placeholder = ledger.records[5];
    expect(user).toMatchObject({ id: "B/user", kind: "user", text: "问题 B", placeholder: null });
    expect(placeholder).toMatchObject({
      id: "B/assistant:placeholder", kind: "assistant", text: "历史回答", resultText: null,
      placeholder: "loading", lane: 1, running: false, isError: false,
      startedAt: BASE + 10_000, endedAt: BASE + 10_000,
    });
    const failed = fixture().map((tn) => (tn.key === "B" ? { ...tn, loadState: "error" as const } : tn));
    const errLedger = buildLedger({ turns: failed, streamTurnKey: "C", liveByStep: LIVE_BY_STEP, nowMs: NOW });
    expect(errLedger.records[5]).toMatchObject({ placeholder: "error", isError: true });
    // fallbackLines 为空时占位文本是空串,不是 undefined。
    const bare = fixture().map((tn) => (tn.key === "B" ? { ...tn, fallbackLines: [] } : tn));
    expect(buildLedger({ turns: bare, streamTurnKey: null, nowMs: NOW }).records[5].text).toBe("");
  });

  it("live turn: unsettled steps append live-assistant/live-tool records with running=true and endedAt=nowMs", () => {
    const ledger = build();
    const byId = new Map(ledger.records.map((r) => [r.id, r]));
    expect(byId.get("C/live-assistant:2")).toMatchObject({
      kind: "assistant", running: true, text: "正在写", lane: 1,
      startedAt: BASE + 5000, endedAt: NOW,
    });
    expect(byId.get("C/live-tool:2:0")).toMatchObject({ kind: "tool", running: true, startedAt: BASE + 5000, endedAt: NOW });
    // 不是流式那一轮就不拼 live 行。
    const other = buildLedger({ turns: fixture(), streamTurnKey: "A", liveByStep: LIVE_BY_STEP, nowMs: NOW });
    expect(other.records.some((r) => r.id.startsWith("C/live-"))).toBe(false);
  });
});

describe("ledgerRecordId", () => {
  it("ledgerRecordId maps think:<seq> to assistant:<seq>", () => {
    expect(ledgerRecordId("run-1", "think:3")).toBe("run-1/assistant:3");
    expect(ledgerRecordId("run-1", "tool:3:0")).toBe("run-1/tool:3:0");
    expect(ledgerRecordId("run-1", "user")).toBe("run-1/user");
  });

  it("ledgerRecordId maps live-think:<step> to live-assistant:<step>", () => {
    // 运行中那一步:中栏过程条的紧凑行是 `live-think:<step>`,账本里同一步是
    // `live-assistant:<step>`(`liveLedgerRows`)—— 不映射的话运行中点「轨迹」
    // 定位不到记录。
    expect(ledgerRecordId("run-1", "live-think:2")).toBe("run-1/live-assistant:2");
    expect(ledgerRecordId("run-1", "live-tool:2:0")).toBe("run-1/live-tool:2:0");
    expect(ledgerRecordId("run-1", "live-assistant:2")).toBe("run-1/live-assistant:2");
  });
});

describe("absoluteSpans", () => {
  it("a row with only serverMs becomes a point span; a degraded turn returns null", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 400, messages: [{ type: "ai", content: "答" }] }, 1000),
      ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }, 1500),
    ];
    const rows = ledgerRowsOf(events, { text: "问", attachmentNames: [], inputs: {} });
    const spans = absoluteSpans(rows, events, null);
    expect(spans).not.toBeNull();
    // retry 是 gantt 的 marker(不进 rows),只能靠自己的 serverMs 落成点块。
    expect(spans?.get("retry:1")).toEqual({ start: BASE + 1500, end: BASE + 1500 });
    expect(spans?.get("assistant:0")).toEqual({ start: BASE + 600, end: BASE + 1000 });
    expect(spans?.get("user")).toEqual({ start: BASE + 600, end: BASE + 600 });

    const bad: SseEvent[] = [upd("agent", { step_count: 1, _duration_ms: 400, messages: [{ type: "ai", content: "答" }] }, null)];
    expect(absoluteSpans(ledgerRowsOf(bad, { text: "问", attachmentNames: [], inputs: {} }), bad, 7)).toBeNull();
  });

  it("with no timed row at all the user span falls back to fallbackStart (null → no entry)", () => {
    const rows = ledgerRowsOf([], { text: "问", attachmentNames: [], inputs: {} });
    expect(absoluteSpans(rows, [], BASE + 42)?.get("user")).toEqual({ start: BASE + 42, end: BASE + 42 });
    expect(absoluteSpans(rows, [], null)?.get("user")).toBeUndefined();
  });
});

describe("lastKnownFrame", () => {
  it("lastKnownFrame returns the last frame with a parseable id", () => {
    const frames: SseEvent[] = [
      { id: `${BASE + 100}-1`, event: "updates", data: {}, rawData: "", receivedAt: "2026-01-01T00:00:00.000Z" },
      { id: `${BASE + 900}-2`, event: "updates", data: {}, rawData: "", receivedAt: "2026-01-01T00:00:01.000Z" },
      { id: null, event: "updates", data: {}, rawData: "", receivedAt: "2026-01-01T00:00:02.000Z" },
    ];
    expect(lastKnownFrame(frames)).toEqual({ serverMs: BASE + 900, receivedAtMs: Date.parse("2026-01-01T00:00:01.000Z") });
    expect(lastKnownFrame([])).toBeNull();
    expect(lastKnownFrame([frames[2]])).toBeNull();
  });
});
