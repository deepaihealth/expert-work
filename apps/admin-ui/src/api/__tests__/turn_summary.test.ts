import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { summarizeTurn } from "../turn_summary";

function updates(messages: unknown[]): SseEvent {
  return {
    id: "u",
    event: "updates",
    data: { agent: { messages } },
    rawData: "",
    receivedAt: "2026-06-29T00:00:00Z",
  };
}

interface AiMsgOpts {
  toolCalls?: number;
  reasoning?: string;
}

/** Build an AIMessage-dump-shaped fixture: ``{type, content, tool_calls?,
 *  additional_kwargs?}``. ``toolCalls: n`` produces ``n`` synthetic calls
 *  (structure only matters — channel logic just checks non-empty). */
function aiMsg(text: string, opts: AiMsgOpts = {}): Record<string, unknown> {
  const msg: Record<string, unknown> = { type: "ai", content: text };
  if (opts.toolCalls) {
    msg.tool_calls = Array.from({ length: opts.toolCalls }, (_, i) => ({
      name: "web_search",
      args: {},
      id: `call_${i}`,
    }));
  }
  if (opts.reasoning !== undefined) {
    msg.additional_kwargs = { reasoning_content: opts.reasoning };
  }
  return msg;
}

function workerEnd(usage: Record<string, unknown> | null): SseEvent {
  const data: Record<string, unknown> = {
    outcome: "success",
    iteration_used: 49,
    llm_call_count: 49,
    wall_clock_ms: 933_000,
  };
  if (usage !== null) data.usage = usage;
  return {
    id: "w",
    event: "worker",
    data: {
      worker_id: "w-1",
      parent_worker_id: null,
      parent_tool_call_id: "call-1",
      label: "spawn_worker",
      agent_ref: "dynamic:general",
      depth: 1,
      kind: "end",
      wseq: 9,
      data,
    },
    rawData: "",
    receivedAt: "2026-06-29T00:00:00Z",
  };
}

describe("summarizeTurn", () => {
  it("sums usage across AI messages and splits cache/reasoning details", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "",
          usage_metadata: {
            input_tokens: 100,
            output_tokens: 20,
            total_tokens: 120,
            input_token_details: { cache_read: 64 },
            output_token_details: { reasoning: 8 },
          },
        },
      ]),
      updates([
        {
          type: "ai",
          content: "final",
          usage_metadata: { input_tokens: 50, output_tokens: 10, total_tokens: 60 },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finalText).toBe("final");
    // The empty first content is skipped; only real texts become segments.
    expect(summary.segments).toEqual([{ text: "final", channel: "final" }]);
    expect(summary.usage).toEqual({
      inputTokens: 150,
      outputTokens: 30,
      totalTokens: 180,
      cacheReadTokens: 64,
      cacheCreationTokens: 0,
      reasoningTokens: 8,
    });
  });

  it("collects reasoning_content blocks in order", () => {
    const events = [
      updates([
        { type: "ai", content: "answer", additional_kwargs: { reasoning_content: "step 1" } },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.reasoning).toEqual(["step 1"]);
    expect(summary.finalText).toBe("answer");
    expect(summary.segments).toEqual([{ text: "answer", channel: "final" }]);
  });

  it("aggregates every AI text into segments (arrival order); only the last (no tool_calls) is final", () => {
    const events = [
      updates([{ type: "ai", content: "step one text" }]),
      updates([{ type: "ai", content: "step two text" }]),
      updates([{ type: "ai", content: "the final text" }]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.segments).toEqual([
      { text: "step one text", channel: "commentary" },
      { text: "step two text", channel: "commentary" },
      { text: "the final text", channel: "final" },
    ]);
    expect(summary.finalText).toBe("the final text");
  });

  it("returns null usage when no AI message reports usage", () => {
    const events = [updates([{ type: "ai", content: "hi" }])];
    const summary = summarizeTurn(events);
    expect(summary.usage).toBeNull();
    expect(summary.finalText).toBe("hi");
    expect(summary.segments).toEqual([{ text: "hi", channel: "final" }]);
  });

  it("takes the highest node step_count and the frame-span latency", () => {
    const events: SseEvent[] = [
      {
        id: "a",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "" }], step_count: 1 } },
        rawData: "",
        receivedAt: "2026-06-29T00:00:00.000Z",
      },
      {
        id: "b",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "done" }], step_count: 3 } },
        rawData: "",
        receivedAt: "2026-06-29T00:00:02.500Z",
      },
    ];
    const summary = summarizeTurn(events);
    expect(summary.stepCount).toBe(3);
    expect(summary.latencyMs).toBe(2500);
  });

  it("prefers the frame ids' server-ms span over receivedAt (replay-safe)", () => {
    // 回放轮的 receivedAt 全挤在回放一瞬间(这里两帧只差 1ms)——时长必须
    // 来自帧 id 的服务端毫秒段,否则 9 分钟的 run 显示成 8ms。
    const events: SseEvent[] = [
      {
        id: "1756170000000-1",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "" }] } },
        rawData: "",
        receivedAt: "2026-08-26T10:00:00.000Z",
      },
      {
        id: "1756170540000-9",
        event: "updates",
        data: { agent: { messages: [{ type: "ai", content: "done" }] } },
        rawData: "",
        receivedAt: "2026-08-26T10:00:00.001Z",
      },
    ];
    expect(summarizeTurn(events).latencyMs).toBe(540_000);
  });

  it("ignores non-updates frames and tool messages", () => {
    const events: SseEvent[] = [
      { id: "m", event: "metadata", data: { run_id: "r" }, rawData: "", receivedAt: "t" },
      updates([{ type: "tool", content: "tool out", tool_call_id: "c1", name: "x" }]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finalText).toBeNull();
    expect(summary.segments).toEqual([]);
    expect(summary.usage).toBeNull();
  });

  it("takes finish_reason and model_name from the last AI message that reports them", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "",
          response_metadata: { finish_reason: "tool_calls", model_name: "glm-5.2" },
        },
      ]),
      updates([
        {
          type: "ai",
          content: "done",
          response_metadata: { finish_reason: "stop", model_name: "glm-5.2" },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.finishReason).toBe("stop");
    expect(summary.modelName).toBe("glm-5.2");
  });

  it("leaves finishReason/modelName null when response_metadata is absent", () => {
    const summary = summarizeTurn([updates([{ type: "ai", content: "hi" }])]);
    expect(summary.finishReason).toBeNull();
    expect(summary.modelName).toBeNull();
  });

  it("sums cache_creation into cacheCreationTokens", () => {
    const events = [
      updates([
        {
          type: "ai",
          content: "x",
          usage_metadata: {
            input_tokens: 10,
            output_tokens: 2,
            total_tokens: 12,
            input_token_details: { cache_read: 4, cache_creation: 6 },
          },
        },
      ]),
    ];
    const summary = summarizeTurn(events);
    expect(summary.usage?.cacheCreationTokens).toBe(6);
    expect(summary.usage?.cacheReadTokens).toBe(4);
  });

  it("emits per-step usage rows keyed by node + step_count without dropping the sum", () => {
    const events: SseEvent[] = [
      {
        id: "a", event: "updates",
        data: { agent: { step_count: 1, messages: [
          { type: "ai", content: "", usage_metadata: { input_tokens: 100, output_tokens: 10, total_tokens: 110 } },
        ] } },
        rawData: "", receivedAt: "2026-07-10T00:00:00Z",
      },
      {
        id: "b", event: "updates",
        data: { agent: { step_count: 2, messages: [
          { type: "ai", content: "done", usage_metadata: { input_tokens: 50, output_tokens: 5, total_tokens: 55 } },
        ] } },
        rawData: "", receivedAt: "2026-07-10T00:00:01Z",
      },
    ];
    const s = summarizeTurn(events);
    expect(s.perStepUsage).toHaveLength(2);
    expect(s.perStepUsage[0]).toEqual({
      node: "agent", stepCount: 1,
      usage: { inputTokens: 100, outputTokens: 10, totalTokens: 110, cacheReadTokens: 0, cacheCreationTokens: 0, reasoningTokens: 0 },
    });
    expect(s.perStepUsage[1].stepCount).toBe(2);
    // summed usage still intact
    expect(s.usage?.totalTokens).toBe(165);
  });

  it("glm 形态:旁白/正文各自与 tool_calls 同帧 → 全 commentary,末条无 tool_calls = final", () => {
    const s = summarizeTurn([
      updates([aiMsg("好的!我将分两章完成", { toolCalls: 1 })]),
      updates([aiMsg("第一章正文…现在撰写第二章", { toolCalls: 1 })]),
      updates([aiMsg("第二章正文,全文完。")]),
    ]);
    expect(s.segments).toEqual([
      { text: "好的!我将分两章完成", channel: "commentary" },
      { text: "第一章正文…现在撰写第二章", channel: "commentary" },
      { text: "第二章正文,全文完。", channel: "final" },
    ]);
    expect(s.finalText).toBe("第二章正文,全文完。");
  });

  it("暂停/未完轮:末条 AI 文本带 tool_calls → 无 final,finalText null", () => {
    const s = summarizeTurn([updates([aiMsg("先搜资料", { toolCalls: 1 })])]);
    expect(s.segments).toEqual([{ text: "先搜资料", channel: "commentary" }]);
    expect(s.finalText).toBeNull();
  });

  it("qwen/doubao 形态:中间轮 content 全空 → 无 commentary 段,只有 final", () => {
    const s = summarizeTurn([
      updates([aiMsg("", { toolCalls: 1, reasoning: "想一想" })]),
      updates([aiMsg("最终答案")]),
    ]);
    expect(s.segments).toEqual([{ text: "最终答案", channel: "final" }]);
    expect(s.reasoning).toEqual(["想一想"]);
  });

  it("deepseek 形态(Minor#6,只钉现状不改渲染):唯一带文本的 AI 消息随 tool_calls 到达,收尾帧 content 转空 → 无 final 段,finalText null,该文本是 commentary", () => {
    const s = summarizeTurn([
      updates([aiMsg("先给出正文,再决定要不要查资料", { toolCalls: 1 })]),
      updates([aiMsg("")]), // 收尾帧 content 转空——textOf 返回 null,不产出新段
    ]);
    expect(s.segments).toEqual([
      { text: "先给出正文,再决定要不要查资料", channel: "commentary" },
    ]);
    expect(s.finalText).toBeNull();
  });
});

describe("summarizeTurn — worker token usage", () => {
  it("counts a worker's tokens into the turn total", () => {
    // 这个循环的第一行历来是 ``if (evt.event !== "updates") continue;``,
    // worker 事件整个被跳过 —— 于是一轮的 token 只算主线。线上实例
    // (run f562fa69):对话页显示 175,137,而同一 trace 下 worker 另有
    // 69 次调用共 3,317,974,少报 19 倍。而同一行并排显示的「总耗时」
    // 是含 worker 的,两个数字口径不一致。
    const events: SseEvent[] = [
      updates([
        {
          type: "ai",
          content: "派个 worker",
          usage_metadata: {
            input_tokens: 170_000,
            output_tokens: 5_137,
            total_tokens: 175_137,
            input_token_details: { cache_read: 1_000, cache_creation: 0 },
            output_token_details: { reasoning: 500 },
          },
        },
      ]),
      workerEnd({
        input_tokens: 3_200_000,
        output_tokens: 117_974,
        total_tokens: 3_317_974,
        input_token_details: { cache_read: 900_000, cache_creation: 7 },
        output_token_details: { reasoning: 20_000 },
      }),
    ];

    const usage = summarizeTurn(events).usage;
    expect(usage).not.toBeNull();
    expect(usage?.inputTokens).toBe(3_370_000);
    expect(usage?.outputTokens).toBe(123_111);
    expect(usage?.totalTokens).toBe(3_493_111);
    expect(usage?.cacheReadTokens).toBe(901_000);
    expect(usage?.reasoningTokens).toBe(20_500);
  });

  it("ignores a worker end frame that reports no usage", () => {
    // provider 可选字段:缺席 ≠ 0。少了这条,「没报」会被当成「免费」。
    const events: SseEvent[] = [
      updates([
        {
          type: "ai",
          content: "hi",
          usage_metadata: { input_tokens: 10, output_tokens: 1, total_tokens: 11 },
        },
      ]),
      workerEnd(null),
    ];
    const usage = summarizeTurn(events).usage;
    expect(usage?.totalTokens).toBe(11);
  });

  it("counts each worker once across a whole delegation tree", () => {
    // 每个 worker 只发一个 end 帧,所以累加所有 worker 帧 = 整棵树,不会
    // 重复。与 duration 那次双计相反(同一段时间既进工具行又进 subagent
    // 行)—— 那个教训在这里正好用来确认不要重蹈覆辙。
    const one = { input_tokens: 100, output_tokens: 10, total_tokens: 110 };
    const events: SseEvent[] = [workerEnd(one), workerEnd(one), workerEnd(one)];
    expect(summarizeTurn(events).usage?.totalTokens).toBe(330);
  });
});

describe("summarizeTurn — worker usage counts the end frame only", () => {
  it("ignores usage on a non-end worker frame", () => {
    // 只有 end 帧是这个 worker 的终局结算。若哪天 update 帧也开始带
    // ``usage``(例如逐步累计),不认 ``kind`` 就会把同一份 token 计很多
    // 遍 —— 这正是过程条耗时踩过的坑(同一段时间既进工具行又进 subagent
    // 行,44m23s vs 真实 23m45s)。这条守着别在 token 上重蹈覆辙。
    const running: SseEvent = {
      id: "w0",
      event: "worker",
      data: {
        worker_id: "w-1",
        kind: "update",
        wseq: 3,
        data: {
          node: "agent",
          usage: { input_tokens: 999_999, output_tokens: 999, total_tokens: 1_000_998 },
        },
      },
      rawData: "",
      receivedAt: "2026-06-29T00:00:00Z",
    };
    const done = workerEnd({ input_tokens: 100, output_tokens: 10, total_tokens: 110 });

    expect(summarizeTurn([running, done]).usage?.totalTokens).toBe(110);
  });
});
