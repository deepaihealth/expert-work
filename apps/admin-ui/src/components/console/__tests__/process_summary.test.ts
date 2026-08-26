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
import { processHeadline, summarizeProcess } from "../process_summary";

const t = (key: string, opts?: Record<string, unknown>): string => i18n.t(key, opts);

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

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
function toolResults(
  results: { id: string; name: string; ok: boolean; durationMs?: number }[],
): SseEvent {
  return upd("tools", {
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

// 3 think rows + 5 tool rows (web_search ×4, http ×1), exactly one of which
// failed. Two deliberate fixture choices: the failing `http` call sits in a
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
  it("counts think / tool rows and builds the tool breakdown by count desc", () => {
    const s = summarizeProcess(compactRowsOf(EVENTS_3THINK_5TOOLS));
    expect(s).toMatchObject({
      think: 3,
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
    expect(summarizeProcess(rows)).toMatchObject({ think: 1, tools: 1, failed: 1 });
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
    const s = summarizeProcess(compactRowsOf(events));
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
    const s = summarizeProcess(compactRowsOf(events));
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
    expect(summarizeProcess(compactRowsOf(events)).durationMs).toBe(1500);
    expect(summarizeProcess([]).durationMs).toBeNull();
    // Rows that all lack a duration read as "unknown" (null), not 0.
    expect(
      summarizeProcess(compactRowsOf([agentStep(1, "只想不干", [])])).durationMs,
    ).toBeNull();
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

  it("renders the spec's full sentence: think · tools(breakdown) · failed tail", () => {
    const s = summarizeProcess(compactRowsOf(EVENTS_3THINK_5TOOLS));
    expect(processHeadline(s, t)).toBe(
      "思考 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败",
    );
  });

  it("headline omits the tool parenthesis without tools and the failed tail without failures", () => {
    expect(
      processHeadline(
        { think: 2, tools: 0, other: 0, failed: 0, toolBreakdown: "", skills: [], durationMs: 1200 },
        t,
      ),
    ).toBe("思考 2 次");
    expect(
      processHeadline(
        { think: 0, tools: 0, other: 0, failed: 0, toolBreakdown: "", skills: [], durationMs: null },
        t,
      ),
    ).toBe("无过程");
  });

  it("counts every non-think / non-tool row (plan / memory / marker …) as 其它 N 步", () => {
    expect(
      processHeadline(
        { think: 0, tools: 0, other: 3, failed: 0, toolBreakdown: "", skills: [], durationMs: null },
        t,
      ),
    ).toBe("其它 3 步");
  });
});
