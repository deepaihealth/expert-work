/**
 * process_summary — the pure counters + one-line headline behind the
 * console's process strip (PR-A.1 Task 3, spec §八.3). Fixtures are real SSE
 * ``updates`` frames run through ``compactRowsOf`` (same ``upd()`` style as
 * TurnBlock.test.tsx), so the counts come from the real projection rather
 * than hand-rolled row objects.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import i18n from "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import { compactRowsOf } from "../../../api/trajectory_rows";
import { modelCallsOf, toolNodeMsOf } from "../live_rows";
import { processHeadline, summarizeProcess } from "../process_summary";

const t = (key: string, opts?: Record<string, unknown>): string => i18n.t(key, opts);

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}
/** 过程条的真实输入:显示的紧凑行 + 这一轮的全部模型调用 + tools 节点帧墙钟(TurnBlock 同款)。 */
function summarize(events: readonly SseEvent[]) {
  return summarizeProcess(
    compactRowsOf(events),
    modelCallsOf(events, undefined),
    toolNodeMsOf(events),
  );
}
const NO_FRAMES: ReadonlyMap<number, number> = new Map();

interface ToolCall {
  id: string;
  name: string;
}

function agentStep(
  step: number,
  reasoning: string | null,
  calls: ToolCall[],
  durationMs?: number,
): SseEvent {
  return upd("agent", {
    step_count: step,
    ...(durationMs === undefined ? {} : { _duration_ms: durationMs }),
    messages: [
      {
        type: "ai",
        content: "",
        ...(reasoning === null
          ? {}
          : { additional_kwargs: { reasoning_content: reasoning } }),
        tool_calls: calls.map((c) => ({ id: c.id, name: c.name, args: { q: c.id } })),
      },
    ],
  });
}
/** `nodeMs` = 这个 tools 节点帧的 `_duration_ms`(这一批调用的墙钟);省略 = 老帧没带。 */
function toolResults(
  results: { id: string; name: string; ok: boolean; durationMs?: number }[],
  nodeMs?: number,
): SseEvent {
  return upd("tools", {
    ...(nodeMs === undefined ? {} : { _duration_ms: nodeMs }),
    messages: results.map((r) => ({
      type: "tool",
      tool_call_id: r.id,
      name: r.name,
      content: r.ok ? "ok" : "boom",
      status: r.ok ? "success" : "error",
      ...(r.durationMs === undefined
        ? {}
        : { additional_kwargs: { duration_ms: r.durationMs } }),
    })),
  });
}

// 4 model calls (3 of them with reasoning → 3 think rows) + 5 tool rows
// (web_search ×4, http ×1), exactly one of which failed. Two deliberate fixture choices: the failing `http` call sits in a
// step with no reasoning of its own so the failure stays a single row
// (`parseTimeline` marks an agent item `hasError` when any of its tools
// errored, which would otherwise paint that step's think row red too), and
// that `http` call comes *first* so the breakdown's count-desc ordering is
// actually exercised rather than matching insertion order by luck.
const EVENTS_3THINK_5TOOLS: SseEvent[] = [
  agentStep(1, null, [{ id: "c1", name: "http" }]),
  toolResults([{ id: "c1", name: "http", ok: false }]),
  agentStep(2, "先查客户资料", [
    { id: "c2", name: "web_search" },
    { id: "c3", name: "web_search" },
  ]),
  toolResults([
    { id: "c2", name: "web_search", ok: true },
    { id: "c3", name: "web_search", ok: true },
  ]),
  agentStep(3, "再补一条", [{ id: "c4", name: "web_search" }]),
  toolResults([{ id: "c4", name: "web_search", ok: true }]),
  agentStep(4, "最后确认", [{ id: "c5", name: "web_search" }]),
  toolResults([{ id: "c5", name: "web_search", ok: true }]),
];

describe("summarizeProcess", () => {
  it("counts model calls / tool rows and builds the tool breakdown by count desc", () => {
    const s = summarize(EVENTS_3THINK_5TOOLS);
    expect(s).toMatchObject({
      // 4 次调用里第 1 步没写思考、不出 think 行 —— 照样算一次(B-202)。
      modelCalls: 4,
      tools: 5,
      other: 0,
      failed: 1,
      toolBreakdown: "web_search ×4 · http ×1",
    });
  });

  it("counts a step's failing tool once — its think row's inherited error status does not add a second failure", () => {
    // `parseTimeline` sets an agent item's `hasError` from its tools
    // (timeline.ts:213), so `trajectory_rows.ts:139` paints this step's think
    // row red as well. That is one failure the user can point at (the tool),
    // not two — 「2 次失败」 for a single failing call is a lie.
    const events: SseEvent[] = [
      agentStep(1, "先查客户资料", [{ id: "c1", name: "http" }]),
      toolResults([{ id: "c1", name: "http", ok: false }]),
    ];
    const rows = compactRowsOf(events);
    // Guard the premise: the think row really is `error` here.
    expect(rows.filter((r) => r.status === "error")).toHaveLength(2);
    expect(summarizeProcess(rows, modelCallsOf(events, undefined), toolNodeMsOf(events))).toMatchObject({
      modelCalls: 1,
      tools: 1,
      failed: 1,
    });
  });

  it("books a skill_view call as skill:<name> in the breakdown and lists the skill", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [
              { id: "s1", name: "skill_view", args: { skill_name: "pptx", path: "SKILL.md" } },
              { id: "s2", name: "skill_view", args: { skill_name: "pptx", path: "ref/a.md" } },
              { id: "c1", name: "web_search", args: { q: "x" } },
            ],
          },
        ],
      }),
      toolResults([
        { id: "s1", name: "skill_view", ok: true },
        { id: "s2", name: "skill_view", ok: true },
        { id: "c1", name: "web_search", ok: true },
      ]),
    ];
    const s = summarize(events);
    expect(s.toolBreakdown).toBe("skill:pptx ×2 · web_search ×1");
    expect(s.skills).toEqual([{ name: "pptx", reads: 2 }]);
  });

  it("a failed skill lookup stays in the breakdown but not in the skills list", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [{ id: "s1", name: "skill_view", args: { skill_name: "gone", path: "SKILL.md" } }],
          },
        ],
      }),
      toolResults([{ id: "s1", name: "skill_view", ok: false }]),
    ];
    const s = summarize(events);
    expect(s.toolBreakdown).toBe("skill:gone ×1");
    expect(s.skills).toEqual([]);
  });

  it("sums row durations treating null as 0, and reports null when no row carries one", () => {
    const events: SseEvent[] = [
      agentStep(1, "想一下", [{ id: "c1", name: "web_search" }], 1000),
      toolResults([{ id: "c1", name: "web_search", ok: true, durationMs: 500 }]),
      // No `_duration_ms` → a think row with `durationMs: null`, which must
      // not poison the sum.
      agentStep(2, "再想一下", []),
    ];
    expect(summarize(events).durationMs).toBe(1500);
    expect(summarizeProcess([], [], NO_FRAMES).durationMs).toBeNull();
    // Rows that all lack a duration read as "unknown" (null), not 0.
    expect(summarize([agentStep(1, "只想不干", [])]).durationMs).toBeNull();
  });

  it("B-254 — splits the turn's time into model, tool and failed-tool time", () => {
    // 10-31 测试对话 099d9b9b:47 分钟里 80% 在模型思考,过程条只给一个合计,看不出来。
    const events: SseEvent[] = [
      agentStep(
        1,
        null,
        [
          { id: "c1", name: "ask_image" },
          { id: "c2", name: "ask_image" },
        ],
        4000,
      ),
      // 两个调用并发跑,这一批的墙钟是 tools 节点帧的 3.2s(B-255:不是 1s + 3s)。
      toolResults(
        [
          { id: "c1", name: "ask_image", ok: true, durationMs: 1000 },
          { id: "c2", name: "ask_image", ok: false, durationMs: 3000 },
        ],
        3200,
      ),
      agentStep(2, "收尾", [], 2000),
    ];
    const s = summarize(events);
    expect(s.modelMs).toBe(6000);
    expect(s.toolMs).toBe(3200);
    expect(s.failedMs).toBe(3000);
    expect(s.durationMs).toBe(9200);
    const empty = summarizeProcess([], [], NO_FRAMES);
    expect(empty.modelMs).toBeNull();
    expect(empty.toolMs).toBeNull();
    expect(empty.failedMs).toBeNull();
  });

  it("B-254 — plan rows land in 'other', not in tool time; untimed failures cost nothing", () => {
    const events: SseEvent[] = [
      agentStep(1, null, [
        { id: "p1", name: "update_plan" },
        { id: "c1", name: "bash" },
      ], 1000),
      toolResults([
        { id: "p1", name: "update_plan", ok: true, durationMs: 2000 },
        { id: "c1", name: "bash", ok: false },
      ]),
    ];
    const s = summarize(events);
    expect(s.modelMs).toBe(1000);
    expect(s.toolMs).toBeNull(); // the only tool row has no time
    expect(s.failedMs).toBeNull();
    expect(s.otherMs).toBe(2000);
    expect(s.failed).toBe(1);
    expect(s.durationMs).toBe(3000);
  });

  it("B-202 — a step that only calls tools (no reasoning → no think row) still counts, time included", () => {
    // glm 系列常见:22 次调用只有 7 次写了思考。没思考的步骤在紧凑投影里不出
    // think 行,原来过程条按行求和,它们的模型耗时整段丢了(会话 0e878fd6
    // 第 1 轮 5m57s vs 总耗时 8m41s)。
    const events: SseEvent[] = [
      agentStep(1, null, [{ id: "c1", name: "web_search" }], 4_000),
      toolResults([{ id: "c1", name: "web_search", ok: true, durationMs: 500 }]),
      agentStep(2, "想一下", [{ id: "c2", name: "web_search" }], 1_000),
      toolResults([{ id: "c2", name: "web_search", ok: true, durationMs: 300 }]),
      agentStep(3, null, [], 2_000),
    ];
    const rows = compactRowsOf(events);
    // Premise: only step 2 projects a think row.
    expect(rows.filter((r) => r.kind === "think")).toHaveLength(1);
    expect(summarizeProcess(rows, modelCallsOf(events, undefined), toolNodeMsOf(events))).toMatchObject({
      modelCalls: 3,
      tools: 2,
      durationMs: 4_000 + 500 + 1_000 + 300 + 2_000,
    });
  });

  it("counts a worker's wall clock once — the subagent row repeats its parent tool's span", () => {
    // ``spawn_worker`` blocks until the worker finishes, so the tool result's
    // ``duration_ms`` already *is* the worker's wall clock (live run f562fa69:
    // tool 938_112ms ↔ worker end frame 933_000ms, the delta being framework
    // overhead). The projection emits both a tool row and a subagent row for
    // that one call (``trajectory_rows.ts``), so summing every row's duration
    // billed the same span twice — that run's strip read 44m23s against a
    // 23m45s wall clock, i.e. "thinking time" exceeding total elapsed.
    const events: SseEvent[] = [
      agentStep(1, "先派个 worker", [{ id: "c1", name: "spawn_worker" }], 1_000),
      ev("worker", {
        worker_id: "w-1", parent_worker_id: null, parent_tool_call_id: "c1",
        label: "撰写员", agent_ref: "dynamic:general", depth: 1, kind: "start",
        wseq: 0, data: { task_excerpt: "写手册", role: null, max_steps: 48 },
      }),
      ev("worker", {
        worker_id: "w-1", parent_worker_id: null, parent_tool_call_id: "c1",
        label: "撰写员", agent_ref: "dynamic:general", depth: 1, kind: "end",
        wseq: 1,
        data: { outcome: "success", iteration_used: 49, llm_call_count: 49, wall_clock_ms: 933_000 },
      }),
      toolResults([{ id: "c1", name: "spawn_worker", ok: true, durationMs: 938_112 }]),
    ];
    const rows = compactRowsOf(events);
    // The subagent row is still there (the strip counts it under 其他) and
    // still carries its own duration for per-row display — only the total
    // must not add it on top of the parent tool's.
    expect(rows.some((r) => r.kind === "subagent" && r.durationMs === 933_000)).toBe(true);
    expect(summarizeProcess(rows, modelCallsOf(events, undefined), toolNodeMsOf(events)).durationMs).toBe(939_112);
  });

  describe("B-255 — parallel tool calls in one step count once, as that step's wall clock", () => {
    // 测试对话 8478cedb:一轮墙钟 34 分钟,几步里各有 2~3 个 ~120s 的 ask_image
    // 并发。过程条逐条相加报「工具 33m14s」,状态栏按 tools 节点帧算的是 18m11s。
    const PARALLEL_3: ToolCall[] = [
      { id: "c1", name: "ask_image" },
      { id: "c2", name: "ask_image" },
      { id: "c3", name: "ask_image" },
    ];

    it("takes the tools-node frame's wall (125s), not the sum of three 120s calls (360s)", () => {
      const events: SseEvent[] = [
        agentStep(1, null, PARALLEL_3, 5_000),
        toolResults(
          PARALLEL_3.map((c) => ({ ...c, ok: true, durationMs: 120_000 })),
          125_000,
        ),
        agentStep(2, "收尾", [], 3_000),
      ];
      const s = summarize(events);
      expect(s.toolMs).toBe(125_000);
      expect(s.modelMs).toBe(8_000);
      expect(s.durationMs).toBe(133_000);
      expect(s.otherMs).toBe(0);
    });

    it("falls back to the step's longest call when the frame carries no duration", () => {
      const events: SseEvent[] = [
        agentStep(1, null, PARALLEL_3, 5_000),
        toolResults([
          { id: "c1", name: "ask_image", ok: true, durationMs: 120_000 },
          { id: "c2", name: "ask_image", ok: true, durationMs: 90_000 },
          { id: "c3", name: "ask_image", ok: true, durationMs: 60_000 },
        ]),
      ];
      const s = summarize(events);
      expect(s.toolMs).toBe(120_000);
      expect(s.durationMs).toBe(125_000);
    });

    it("sequential steps add up — each its own wall", () => {
      const events: SseEvent[] = [
        agentStep(1, null, PARALLEL_3, 5_000),
        toolResults(
          PARALLEL_3.map((c) => ({ ...c, ok: true, durationMs: 120_000 })),
          125_000,
        ),
        agentStep(2, null, [{ id: "c4", name: "web_search" }], 2_000),
        toolResults([{ id: "c4", name: "web_search", ok: true, durationMs: 40_000 }], 41_000),
        // 第 3 步的帧没带时长 → 退回它自己最长的那条。
        agentStep(3, null, [{ id: "c5", name: "http" }, { id: "c6", name: "http" }], 1_000),
        toolResults([
          { id: "c5", name: "http", ok: true, durationMs: 7_000 },
          { id: "c6", name: "http", ok: true, durationMs: 9_000 },
        ]),
      ];
      expect(summarize(events).toolMs).toBe(125_000 + 41_000 + 9_000);
    });

    it("a failed call inside a parallel step never costs more than that step's tool time", () => {
      // 三个 120s 的调用两个失败:失败之和 240s,但这一步的工具只花了 125s。
      const events: SseEvent[] = [
        agentStep(1, null, PARALLEL_3, 5_000),
        toolResults(
          [
            { id: "c1", name: "ask_image", ok: false, durationMs: 120_000 },
            { id: "c2", name: "ask_image", ok: false, durationMs: 120_000 },
            { id: "c3", name: "ask_image", ok: true, durationMs: 120_000 },
          ],
          125_000,
        ),
        // 单个失败、帧没带时长:失败那段 = 它自己的 30s(不超过这一步兜底的 50s)。
        agentStep(2, null, [{ id: "c4", name: "http" }, { id: "c5", name: "http" }], 1_000),
        toolResults([
          { id: "c4", name: "http", ok: false, durationMs: 30_000 },
          { id: "c5", name: "http", ok: true, durationMs: 50_000 },
        ]),
      ];
      const s = summarize(events);
      expect(s.failed).toBe(3);
      expect(s.toolMs).toBe(125_000 + 50_000);
      expect(s.failedMs).toBe(125_000 + 30_000);
    });

    it("keeps 模型 + 工具 + 其他 = 合计 with other rows and a worker in the turn", () => {
      const events: SseEvent[] = [
        agentStep(1, null, [{ id: "p1", name: "update_plan" }], 1_000),
        toolResults([{ id: "p1", name: "update_plan", ok: true, durationMs: 200 }], 250),
        agentStep(2, null, PARALLEL_3, 5_000),
        toolResults(
          PARALLEL_3.map((c) => ({ ...c, ok: true, durationMs: 120_000 })),
          125_000,
        ),
        agentStep(3, null, [{ id: "w1", name: "spawn_worker" }, { id: "w2", name: "spawn_worker" }], 2_000),
        ev("worker", {
          worker_id: "w-1", parent_worker_id: null, parent_tool_call_id: "w1",
          label: "甲", agent_ref: "dynamic:general", depth: 1, kind: "start", wseq: 0,
          data: { task_excerpt: "查资料", role: null, max_steps: 8 },
        }),
        ev("worker", {
          worker_id: "w-1", parent_worker_id: null, parent_tool_call_id: "w1",
          label: "甲", agent_ref: "dynamic:general", depth: 1, kind: "end", wseq: 1,
          data: { outcome: "success", iteration_used: 3, llm_call_count: 3, wall_clock_ms: 60_000 },
        }),
        toolResults(
          [
            { id: "w1", name: "spawn_worker", ok: true, durationMs: 61_000 },
            { id: "w2", name: "spawn_worker", ok: true, durationMs: 58_000 },
          ],
          62_000,
        ),
      ];
      // Premise: the worker really projects a timed subagent row (excluded from the sums).
      expect(
        compactRowsOf(events).some((r) => r.kind === "subagent" && r.durationMs === 60_000),
      ).toBe(true);
      const s = summarize(events);
      expect(s.modelMs).toBe(8_000);
      expect(s.toolMs).toBe(125_000 + 62_000);
      expect(s.otherMs).toBe(200); // the update_plan row
      expect(s.durationMs).toBe((s.modelMs ?? 0) + (s.toolMs ?? 0) + s.otherMs);
      expect(s.durationMs).toBe(8_000 + 187_000 + 200);
    });
  });
});

describe("processHeadline", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders the spec's full sentence: model calls · tools(breakdown) · failed tail", () => {
    const s = summarize(EVENTS_3THINK_5TOOLS);
    expect(processHeadline(s, t)).toBe(
      "模型调用 4 次 · 工具 5 次（web_search ×4 · http ×1） · 1 次失败",
    );
  });

  it("headline omits the tool parenthesis without tools and the failed tail without failures", () => {
    expect(
      processHeadline(
        { modelCalls: 2, tools: 0, other: 0, failed: 0, toolBreakdown: "", skills: [], durationMs: 1200, modelMs: null, toolMs: null, failedMs: null, otherMs: 0 },
        t,
      ),
    ).toBe("模型调用 2 次");
    expect(
      processHeadline(
        { modelCalls: 0, tools: 0, other: 0, failed: 0, toolBreakdown: "", skills: [], durationMs: null, modelMs: null, toolMs: null, failedMs: null, otherMs: 0 },
        t,
      ),
    ).toBe("无过程");
  });

  it("counts every non-model-call / non-tool row （plan / memory / marker …） as 其他 N 步", () => {
    expect(
      processHeadline(
        { modelCalls: 0, tools: 0, other: 3, failed: 0, toolBreakdown: "", skills: [], durationMs: null, modelMs: null, toolMs: null, failedMs: null, otherMs: 0 },
        t,
      ),
    ).toBe("其他 3 步");
  });
});
