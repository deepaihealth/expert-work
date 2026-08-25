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
  [2, { content: "正在写", reasoning: "想想", toolNames: new Map([["call_a", "calc"]]), reasoningMs: null }],
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

/** PR-A.3 §十.1 —— 一条带 system_prompt 帧的轮:metadata + system_prompt{text} +
 *  一个终答步(供折叠 / turnStart 测试复用)。`seq` 每次调用自增,调用方按数组
 *  顺序传给 `buildLedger` 就行。 */
let systemTurnSeq = 0;
function turnWithSystem(key: string, text: string): ConsoleTurn {
  const seq = systemTurnSeq;
  systemTurnSeq += 1;
  const events: SseEvent[] = [
    ev("metadata", { run_id: `run-${key}`, thread_id: "t" }, 0),
    ev("system_prompt", { text }, 100),
    upd("agent", { step_count: 1, _duration_ms: 200, messages: [{
      type: "ai", content: "答", usage_metadata: { input_tokens: 10, output_tokens: 2 },
    }] }, 500),
  ];
  return turnOf({
    key, seq,
    turn: { id: key, input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null },
  });
}

describe("buildLedger", () => {
  it("records are in turn order with turnStart/turnEnd and 0-based index", () => {
    const ledger = build();
    expect(ledger.records.map((r) => r.id)).toEqual([
      "A/user", "A/assistant:0", "A/tool:0:0", "A/assistant:1",
      "B/user", "B/assistant:placeholder",
      "C/user", "C/assistant:0", "C/tool:0:0", "C/live-assistant:2", "C/live-tool:2:call_a",
    ]);
    expect(ledger.records.map((r) => r.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(ledger.records.filter((r) => r.turnStart).map((r) => r.id)).toEqual(["A/user", "B/user", "C/user"]);
    expect(ledger.records.filter((r) => r.turnEnd).map((r) => r.id)).toEqual([
      "A/assistant:1", "B/assistant:placeholder", "C/live-tool:2:call_a",
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
    expect(byId.get("C/live-tool:2:call_a")).toMatchObject({ ownerRequestNo: 4, parentId: "C/live-assistant:2" });
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

  // 终审 M13 —— 记忆写回 / 反思是某一步 agent 之后发生的事,详情里那条
  // 「Assistant Message ›」靠 `parentId` 出;原先一律 null,链接永远不出现。
  it("memory 写回与 reflect 挂在同轮之前最近的 assistant 上(它之前没有 → null)", () => {
    const events: SseEvent[] = [
      upd("memory_recall", { recalled_memories: [{ id: "m1", content: "老客户 A" }] }, 100),
      upd("agent", { step_count: 1, _duration_ms: 100, messages: [{ type: "ai", content: "做点事" }] }, 300),
      upd("reflect", { reflections: [{ verdict: "revise", critique: "漏了夜间" }] }, 700),
      upd("memory_writeback", { written_memories: [{ id: "w1", content: "写回内容" }] }, 800),
    ];
    const turns = [turnOf({ key: "L", seq: 0, turn: { id: "L", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const byId = new Map(buildLedger({ turns, streamTurnKey: null, nowMs: NOW }).records.map((r) => [r.id, r]));

    expect(byId.get("L/assistant:1")).toBeDefined();
    expect(byId.get("L/reflect:2")?.parentId).toBe("L/assistant:1");
    expect(byId.get("L/memory:3")?.parentId).toBe("L/assistant:1");
    // 召回发生在第一步之前 —— 没有可挂的 assistant。
    expect(byId.get("L/memory:0")?.parentId).toBeNull();
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
    expect(byId.get("C/live-tool:2:call_a")).toMatchObject({ kind: "tool", running: true, startedAt: BASE + 5000, endedAt: NOW });
    // 不是流式那一轮就不拼 live 行。
    const other = buildLedger({ turns: fixture(), streamTurnKey: "A", liveByStep: LIVE_BY_STEP, nowMs: NOW });
    expect(other.records.some((r) => r.id.startsWith("C/live-"))).toBe(false);
  });
});

describe("SYSTEM row (PR-A.3 §十.1)", () => {
  it("lane 0, content = first line, turnStart on the SYSTEM record, USER right after", () => {
    const ledger = buildLedger({ turns: [turnWithSystem("t1", "你是评审员\n只说重点")], streamTurnKey: null, nowMs: NOW });
    expect(ledger.records.slice(0, 2).map((r) => [r.kind, r.lane, r.turnStart, r.text])).toEqual([
      ["system", 0, true, "你是评审员"], ["user", 0, false, expect.any(String)],
    ]);
  });

  // 终审 I1 —— USER 的时长钉点曾把 SYSTEM 行也算进 `spans`(它是「非 user 且
  // 有 serverMs」的一行),USER 因此被钉到 SYSTEM 帧的同一毫秒、两块在时长
  // 模式下同泳道同 x 完全重叠(SYSTEM 点不到 / 悬停不到)。USER 应回到本轮
  // 首条「步」的起点(与 assistant 首步同一时刻),SYSTEM 严格更早。
  it("SYSTEM's absolute span does not pull USER onto the same instant as SYSTEM", () => {
    const ledger = buildLedger({ turns: [turnWithSystem("t1", "你是评审员")], streamTurnKey: null, nowMs: NOW });
    const system = ledger.records.find((r) => r.kind === "system")!;
    const user = ledger.records.find((r) => r.kind === "user")!;
    const assistant = ledger.records.find((r) => r.kind === "assistant")!;
    expect(system.startedAt).not.toBeNull();
    expect(user.startedAt).not.toBeNull();
    expect(system.startedAt! < user.startedAt!).toBe(true);
    expect(user.startedAt).toBe(assistant.startedAt);
  });

  it("consecutive turns with the same prompt fold it; a changed prompt shows again", () => {
    const ledger = buildLedger({
      turns: [turnWithSystem("t1", "A"), turnWithSystem("t2", "A"), turnWithSystem("t3", "B"), turnWithSystem("t4", "B")],
      streamTurnKey: null, nowMs: NOW,
    });
    const systems = ledger.records.filter((r) => r.kind === "system").map((r) => [r.turnKey, r.text]);
    expect(systems).toEqual([["t1", "A"], ["t3", "B"]]);
    // 折掉的轮,USER 仍是 turnStart。
    expect(ledger.records.find((r) => r.turnKey === "t2")).toMatchObject({ kind: "user", turnStart: true });
  });

  // PR-A.3 follow-up(T9 deferred minor)—— 夹在中间的未回放轮(一帧都没有,
  // 提示词未知)不打断折叠:它不碰「上一条 SYSTEM 文本」,后一轮同提示词照折;
  // 等它回放完 buildLedger 会整个重算,届时再按真实提示词定。
  it("an unreplayed turn between two same-prompt turns does not break the fold", () => {
    const middle = turnOf({
      key: "mid", seq: 50, loadState: "loading", createdAt: CREATED_B,
      turn: { id: "mid", input: "问", attachments: [], inputs: {}, events: [], status: "done", error: null, approval: null },
    });
    const ledger = buildLedger({
      turns: [turnWithSystem("s1", "A"), middle, turnWithSystem("s3", "A"), turnWithSystem("s4", "B")],
      streamTurnKey: null, nowMs: NOW,
    });
    const systems = ledger.records.filter((r) => r.kind === "system").map((r) => [r.turnKey, r.text]);
    expect(systems).toEqual([["s1", "A"], ["s4", "B"]]);
    expect(ledger.records.filter((r) => r.turnKey === "mid").map((r) => r.kind)).toEqual(["user", "assistant"]);
  });

  it("firstTokenAt = startedAt + firstTokenMs (clamped to endedAt) and LedgerRequest.firstTokenMs", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 2000, messages: [{
        type: "ai", content: "答案",
        additional_kwargs: { first_token_ms: 500 },
        usage_metadata: { input_tokens: 10, output_tokens: 2 },
      }] }, 3000),
      // 首 token 偏移比这一步自身跨度还大 —— 必须夹到 endedAt,不能算出超出块外的时刻。
      upd("agent", { step_count: 2, _duration_ms: 100, messages: [{
        type: "ai", content: "结论",
        additional_kwargs: { first_token_ms: 9999 },
        usage_metadata: { input_tokens: 5, output_tokens: 1 },
      }] }, 3200),
      // 没带 first_token_ms 的步 → null。
      upd("agent", { step_count: 3, _duration_ms: 50, messages: [{
        type: "ai", content: "追加",
        usage_metadata: { input_tokens: 1, output_tokens: 1 },
      }] }, 3300),
    ];
    const turns = [turnOf({ key: "F", seq: 0, turn: { id: "F", input: "问", attachments: [], inputs: {}, events, status: "done", error: null, approval: null } })];
    const ledger = buildLedger({ turns, streamTurnKey: null, nowMs: NOW });
    const assistants = ledger.records.filter((r) => r.kind === "assistant");
    expect(assistants[0].firstTokenAt).toBe(assistants[0].startedAt! + 500);
    expect(assistants[1].firstTokenAt).toBe(assistants[1].endedAt);
    expect(assistants[2].firstTokenAt).toBeNull();
    // Minor 2 —— `LedgerRequest.firstTokenMs` 现在与 `firstTokenAt` 同口径
    // 夹取:第二步 9999 偏移已被夹到 `endedAt`(见上一条断言),这里的
    // 100 = 夹后差值(该步 `_duration_ms: 100`),不是原始 9999,否则悬停
    // 提示与「请求 #N · 首 token」会报出两个不同的数(终审 Minor 2)。
    expect(ledger.requests.map((r) => r.firstTokenMs)).toEqual([500, 100, null]);
  });
});

/** 被中止的轮:工具调用发出去了,ToolMessage 永远没到 —— 回放之后这条 tool 行
 *  始终是 running。窗口里躺着这么一条历史记录,是「最后一条 running 拉到现在」
 *  那条规则最容易踩的坑。 */
function abortedEvents(atMs: number | null): SseEvent[] {
  return [
    upd("agent", { step_count: 1, _duration_ms: 200, messages: [{
      type: "ai", content: "查一下",
      usage_metadata: { input_tokens: 10, output_tokens: 2 },
      tool_calls: [call("c7", "query_crm")],
    }] }, atMs),
  ];
}
function abortedTurn(key: string, seq: number, atMs: number | null, over: Partial<ConsoleTurn> = {}): ConsoleTurn {
  return turnOf({
    key, seq,
    turn: { id: key, input: "问", attachments: [], inputs: {}, events: abortedEvents(atMs), status: "error", error: null, approval: null },
    ...over,
  });
}

describe("buildLedger · 运行中尾块", () => {
  it("a history turn's still-running record is NOT stretched to nowMs", () => {
    const ledger = buildLedger({ turns: [abortedTurn("H", 0, 2000)], streamTurnKey: null, nowMs: NOW });
    const byId = new Map(ledger.records.map((r) => [r.id, r]));
    expect(byId.get("H/tool:0:0")).toMatchObject({ running: true, startedAt: BASE + 2000, endedAt: BASE + 2000 });
    expect(ledger.records.some((r) => r.endedAt === NOW)).toBe(false);

    // 另一半:正在流的那轮自己**没有** running 记录时,窗口里最后一条 running
    // 仍然是历史轮那条 —— 它照样不许被顶到「现在」。
    const settledStream = turnOf({ key: "S", seq: 1, source: "live",
      turn: { id: "S", input: "问", attachments: [], inputs: {}, events: turnAEvents(), status: "running", error: null, approval: null } });
    const withStream = buildLedger({ turns: [abortedTurn("H", 0, 2000), settledStream], streamTurnKey: "S", nowMs: NOW });
    expect(withStream.records.some((r) => r.running && r.turnKey === "S")).toBe(false);
    expect(withStream.records.find((r) => r.id === "H/tool:0:0")).toMatchObject({ running: true, endedAt: BASE + 2000 });
    expect(withStream.records.some((r) => r.endedAt === NOW)).toBe(false);
  });

  it("only the stream turn's last running record is stretched to nowMs", () => {
    const turns = [
      abortedTurn("H", 0, 2000),
      abortedTurn("S", 1, 6000, { source: "live", turn: { id: "S", input: "问", attachments: [], inputs: {}, events: abortedEvents(6000), status: "running", error: null, approval: null } }),
    ];
    const byId = new Map(buildLedger({ turns, streamTurnKey: "S", nowMs: NOW }).records.map((r) => [r.id, r]));
    expect(byId.get("H/tool:0:0")).toMatchObject({ running: true, endedAt: BASE + 2000 });
    expect(byId.get("S/tool:0:0")).toMatchObject({ running: true, startedAt: BASE + 6000, endedAt: NOW });
  });

  it("a stream turn with no usable times is not stretched — both ends stay null", () => {
    const turns = [abortedTurn("S", 0, null, { source: "live", turn: { id: "S", input: "问", attachments: [], inputs: {}, events: abortedEvents(null), status: "running", error: null, approval: null } })];
    const ledger = buildLedger({ turns, streamTurnKey: "S", nowMs: NOW });
    const rec = ledger.records.find((r) => r.id === "S/tool:0:0");
    expect(rec).toMatchObject({ running: true, startedAt: null, endedAt: null });
    expect(ledger.timed).toBe(false);
  });

  it("live rows start at the turn's latest known end (not the last row in order), clamped to nowMs", () => {
    // 两个并行工具:c1 的结果 1400 才回来,c2 的 1300 就回来了 —— 按行序的最后
    // 一条(c2)结束得更早,所以起点必须取「最大的已知结束时刻」。
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 400, messages: [{ type: "ai", content: "", tool_calls: [call("c1", "a"), call("c2", "b")] }] }, 1000),
      upd("tools", { messages: [result("c1", "a", 300)] }, 1400),
      upd("tools", { messages: [result("c2", "b", 200)] }, 1300),
    ];
    const turns = [turnOf({ key: "P", seq: 0, source: "live", turn: { id: "P", input: "问", attachments: [], inputs: {}, events, status: "running", error: null, approval: null } })];
    const live: ReadonlyMap<number, LiveStep> = new Map<number, LiveStep>([
      [2, { content: "写", reasoning: "", toolNames: new Map(), reasoningMs: null }],
    ]);
    const byId = new Map(buildLedger({ turns, streamTurnKey: "P", liveByStep: live, nowMs: NOW }).records.map((r) => [r.id, r]));
    expect(byId.get("P/tool:0:1")).toMatchObject({ endedAt: BASE + 1300 });
    expect(byId.get("P/live-assistant:2")).toMatchObject({ startedAt: BASE + 1400, endedAt: NOW });

    // 调用方没校准过时钟(传进来的 now 落在已知结束时刻之前)→ 起点夹到 now,
    // 不能出现 start > end 的倒挂块。
    const skewed = buildLedger({ turns, streamTurnKey: "P", liveByStep: live, nowMs: BASE + 1200 });
    expect(skewed.records.find((r) => r.id === "P/live-assistant:2")).toMatchObject({
      startedAt: BASE + 1200, endedAt: BASE + 1200,
    });
  });

  it("an unreplayed turn without a parseable createdAt has null times and flips ledger.timed", () => {
    for (const createdAt of [null, "不是时间"]) {
      const turns = [turnOf({ key: "B", seq: 0, loadState: "pending", createdAt,
        fallbackLines: [{ text: "答", channel: "final" }],
        turn: { id: "B", input: "问", attachments: [], inputs: {}, events: [], status: "done", error: null, approval: null } })];
      const ledger = buildLedger({ turns, streamTurnKey: null, nowMs: NOW });
      expect(ledger.records.map((r) => [r.startedAt, r.endedAt])).toEqual([[null, null], [null, null]]);
      expect(ledger.timed).toBe(false);
    }
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

  // 终审 I1 —— 运行中账本每 rAF 重建一次,历史轮的 `events` 引用整个会话不变,
  // 所以每轮的 gantt 只该跑第一次。
  it("同一份 events 引用重复调用命中缓存;换引用 / 换 fallbackStart 都重算", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, _duration_ms: 400, messages: [{ type: "ai", content: "答" }] }, 1000),
    ];
    const input = { text: "问", attachmentNames: [], inputs: {} };
    const rows = ledgerRowsOf(events, input);
    const first = absoluteSpans(rows, events, null);
    expect(first).not.toBeNull();
    // 同一引用 + 同一 fallbackStart → 原样给回上次那个 Map。
    expect(absoluteSpans(ledgerRowsOf(events, input), events, null)).toBe(first);
    // 内容相同但换了数组引用 → 另一轮的账,不共用。
    expect(absoluteSpans(rows, [...events], null)).not.toBe(first);
    // `fallbackStart` 是唯一不由 events 决定的入参(USER 行的兜底起点),
    // 它变了必须重算,否则共用一个空 events 的两轮会互相串起点。
    const emptyEvents: SseEvent[] = [];
    const emptyRows = ledgerRowsOf(emptyEvents, input);
    expect(absoluteSpans(emptyRows, emptyEvents, BASE + 42)?.get("user"))
      .toEqual({ start: BASE + 42, end: BASE + 42 });
    expect(absoluteSpans(emptyRows, emptyEvents, BASE + 99)?.get("user"))
      .toEqual({ start: BASE + 99, end: BASE + 99 });
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
