/**
 * 泳道 v2 数据层测试(PR-A.1 Task 5)—— 双投影模型 `laneProjection` /
 * `rangeToRowSpan`。PR-A 的三条 `it` 行为已被 spec §八.7 改写(泳道从「gantt
 * 块」变成「每条轨迹行一个块」),断言随之重写,逐条对照见 task-5-report.md。
 */
import { describe, expect, it } from "vitest";

import type { SseEvent } from "../../../api/sessions";
import { buildGanttRows } from "../../../api/gantt_timeline";
import { trajectoryRowsOf, type TrajectoryRow } from "../../../api/trajectory_rows";
import { LANE_OF_KIND, laneProjection, rangeToRowSpan, type Lane } from "../lane_strip_model";

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
function call(id: string, name: string) {
  return { id, name, args: {} };
}
function result(id: string, name: string, durationMs: number, status: "success" | "error" = "success") {
  return {
    type: "tool",
    tool_call_id: id,
    name,
    content: status === "error" ? "炸了" : "ok",
    status,
    additional_kwargs: { duration_ms: durationMs },
  };
}

const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

/** 9 行:user / think / tool / tool / think / tool / tool(error) / think / assistant。 */
function nineRowEvents(): SseEvent[] {
  return [
    upd("agent", {
      step_count: 1,
      _duration_ms: 400,
      messages: [{
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "先查客户档案" },
        tool_calls: [call("c1", "query_crm"), call("c2", "search")],
      }],
    }, 1000),
    upd("tools", { messages: [result("c1", "query_crm", 300)] }, 1400),
    upd("tools", { messages: [result("c2", "search", 200)] }, 1700),
    upd("agent", {
      step_count: 2,
      _duration_ms: 500,
      messages: [{
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "再算一次" },
        tool_calls: [call("c3", "calc"), call("c4", "boom")],
      }],
    }, 2300),
    upd("tools", { messages: [result("c3", "calc", 100)] }, 2400),
    upd("tools", { messages: [result("c4", "boom", 150, "error")] }, 2600),
    upd("agent", { step_count: 3, _duration_ms: 600, messages: [{ type: "ai", content: "结论" }] }, 3200),
  ];
}
function nineRows(events: SseEvent[]): TrajectoryRow[] {
  return trajectoryRowsOf(events, INPUT, "结论", "done");
}

describe("laneProjection · sequence", () => {
  it("sequence: one block per row, equal width, lane by kind, ticks every ceil(N/6) rows", () => {
    const events = nineRowEvents();
    const rows = nineRows(events);
    expect(rows).toHaveLength(9);

    const p = laneProjection(rows, events, { mode: "sequence", running: false, nowMs: 0 });

    expect(p.mode).toBe("sequence");
    expect(p.degraded).toBe(false);
    expect(p.total).toBe(9);
    expect(p.blocks).toHaveLength(9);
    expect(p.blocks.map((b) => [b.start, b.end])).toEqual(
      [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9]],
    );
    expect(p.blocks.map((b) => b.rowIndex)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(p.blocks.map((b) => b.rowId)).toEqual(rows.map((r) => r.id));
    expect(p.blocks.map((b) => b.lane)).toEqual(
      ["user", "model", "tools", "tools", "model", "tools", "tools", "model", "model"],
    );
    // 每 ceil(9/6) = 2 行一个刻度,首尾必有 → #1 #3 #5 #7 #9。
    // (task-5-brief 的 `it` 标题写的是 #1/#4/#7/#9,那对应 step=3=ceil(N/3);
    //  这里按 brief 正文的算法 ceil(N/6) 实现,分歧记在 task-5-report.md。)
    expect(p.ticks).toEqual([
      { at: 0, label: "#1" }, { at: 2, label: "#3" }, { at: 4, label: "#5" },
      { at: 6, label: "#7" }, { at: 8, label: "#9" },
    ]);
  });

  it("sequence: a tool row with status=error is hasError", () => {
    const events = nineRowEvents();
    const rows = nineRows(events);
    const p = laneProjection(rows, events, { mode: "sequence", running: false, nowMs: 0 });

    // 失败的工具行,外加它所属的 think 步(`AgentStep.hasError` 会把整步标红,
    // `trajectoryRowsOf` 据此给 think 行 `status: "error"`)。
    const errored = p.blocks.filter((b) => b.hasError);
    expect(errored.map((b) => [b.kind, b.rowIndex])).toEqual([["think", 5], ["tool", 7]]);
    const row = rows[6];
    expect(row.kind === "tool" && row.entry.toolName).toBe("boom");
    expect(p.blocks.filter((b) => !b.hasError).map((b) => b.rowIndex)).toEqual([1, 2, 3, 4, 6, 8, 9]);
  });
});

describe("laneProjection · duration", () => {
  it("duration: blocks carry gantt ms; user is a point at 0 and assistant a point at total", () => {
    const events = nineRowEvents();
    const rows = nineRows(events);
    const gantt = buildGanttRows(events, { settled: true });
    const p = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });

    expect(p.mode).toBe("duration");
    expect(p.degraded).toBe(false);
    expect(p.total).toBe(gantt.totalMs);
    expect(p.total).toBe(2600);
    expect(p.blocks).toHaveLength(9);

    const byRow = new Map(p.blocks.map((b) => [b.rowId, b]));
    expect(byRow.get("user")).toMatchObject({ start: 0, end: 0 });
    expect(byRow.get("assistant")).toMatchObject({ start: 2600, end: 2600 });
    // gantt 绝对轴 t0 = BASE+600(首个 agent 步 1000ms 结束、耗时 400ms)。
    expect(byRow.get("think:0")).toMatchObject({ start: 0, end: 400 });
    expect(byRow.get("tool:0:0")).toMatchObject({ start: 500, end: 800 });
    expect(byRow.get("tool:0:1")).toMatchObject({ start: 900, end: 1100 });
    expect(byRow.get("think:1")).toMatchObject({ start: 1200, end: 1700 });
    expect(byRow.get("tool:1:1")).toMatchObject({ start: 1850, end: 2000, hasError: true });
    expect(byRow.get("think:2")).toMatchObject({ start: 2000, end: 2600 });

    expect(p.ticks.map((tk) => tk.at)).toEqual([0, 650, 1300, 1950, 2600]);
    expect(p.ticks[0].label).toBe("0ms");
    expect(p.ticks[4].label).toBe("2.6s");
  });

  it("duration: a marker row placed by serverMs is a point block; a row with no timing at all is not drawn", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 400,
        messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "想" }, tool_calls: [] }],
      }, 1000),
      ev("compaction", { passes: 1, tokens_before: 100, tokens_after: 50, summary_chars: 10 }, 1500),
      // 没有帧 id → 该 marker 行既无 gantt 时序也无 serverMs(marker 不参与
      // gantt 的 degraded 判定,所以本例仍走非降级的时长排布)。
      ev("compaction", { passes: 2, tokens_before: 80, tokens_after: 40, summary_chars: 8 }, null),
      upd("agent", { step_count: 2, _duration_ms: 300, messages: [{ type: "ai", content: "答" }] }, 2000),
    ];
    const rows = trajectoryRowsOf(events, INPUT, "答", "done");
    const p = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });

    expect(p.degraded).toBe(false);
    const markers = rows.filter((r) => r.kind === "compaction");
    expect(markers).toHaveLength(2);
    expect(markers[0].serverMs).toBe(BASE_MS + 1500);
    expect(markers[1].serverMs).toBeNull();

    const drawn = p.blocks.find((b) => b.rowId === markers[0].id);
    expect(drawn).toBeDefined();
    expect(drawn?.start).toBe(900);
    expect(drawn?.end).toBe(900);
    expect(drawn?.lane).toBe("user");
    expect(p.blocks.some((b) => b.rowId === markers[1].id)).toBe(false);
  });

  it("duration: with no parseable timing it degrades to sequence layout and flags degraded", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "想" }, tool_calls: [] }],
      }, null),
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "答" }] }, null),
    ];
    const rows = trajectoryRowsOf(events, INPUT, "答", "done");
    const p = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });

    expect(buildGanttRows(events, { settled: true }).degraded).toBe(true);
    expect(p.degraded).toBe(true);
    expect(p.mode).toBe("duration");
    expect(p.total).toBe(rows.length);
    expect(p.blocks.map((b) => [b.start, b.end])).toEqual(
      rows.map((_r, i) => [i, i + 1]),
    );
    expect(p.ticks[0].label).toBe("#1");
  });

  it("running: the unsettled tail block is live and total grows with nowMs", () => {
    const R = "2026-01-01T00:00:00.000Z";
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 500,
        messages: [{
          type: "ai",
          content: "",
          additional_kwargs: { reasoning_content: "查" },
          tool_calls: [call("c1", "query_crm")],
        }],
      }, 1000, R),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "running");
    expect(rows.map((r) => r.id)).toEqual(["user", "think:0", "tool:0:0"]);

    const settled = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });
    const nowMs = Date.parse(R) + 5000;
    const live = laneProjection(rows, events, { mode: "duration", running: true, nowMs });

    expect(live.total).toBe(settled.total + 5000);
    expect(live.blocks.map((b) => b.live)).toEqual([false, false, true]);
    const tail = live.blocks[2];
    expect(tail.rowId).toBe("tool:0:0");
    expect(tail.end).toBe(live.total);
    // 顺序模式下同一条尾块也是 live(不受 gantt 时序有无影响)。
    const seq = laneProjection(rows, events, { mode: "sequence", running: true, nowMs });
    expect(seq.blocks.map((b) => b.live)).toEqual([false, false, true]);
  });
});

describe("rangeToRowSpan / LANE_OF_KIND", () => {
  it("rangeToRowSpan maps a domain interval to the intersecting 1-based row span, null when empty", () => {
    const events = nineRowEvents();
    const rows = nineRows(events);
    const p = laneProjection(rows, events, { mode: "sequence", running: false, nowMs: 0 });

    expect(rangeToRowSpan(p, 3, 6)).toEqual({ from: 4, to: 6 });
    // 参数顺序无关(反向拖选)。
    expect(rangeToRowSpan(p, 6, 3)).toEqual({ from: 4, to: 6 });
    expect(rangeToRowSpan(p, 0, 9)).toEqual({ from: 1, to: 9 });
    expect(rangeToRowSpan(p, 20, 30)).toBeNull();

    // 时长模式下点块(user 在 0)也能被选中区间命中。
    const d = laneProjection(rows, events, { mode: "duration", running: false, nowMs: 0 });
    expect(rangeToRowSpan(d, 0, 100)).toEqual({ from: 1, to: 2 });
    expect(rangeToRowSpan(d, 10_000, 20_000)).toBeNull();
  });

  it("LANE_OF_KIND covers every TrajectoryRow kind", () => {
    const KINDS = [
      "user", "think", "tool", "subagent", "plan", "memory", "reflect",
      "compaction", "retry", "error", "approval", "guard", "gap", "assistant",
    ] as const;
    // 编译期穷尽闸:`KINDS` 少一个 kind → `MissingKind` 不是 never → 下面的
    // 赋值不过;多一个 → `ExtraKind` 不是 never。`LANE_OF_KIND` 自身的
    // `Record<TrajectoryRow["kind"], Lane>` 标注是第二道(缺键即类型错)。
    type MissingKind = Exclude<TrajectoryRow["kind"], (typeof KINDS)[number]>;
    type ExtraKind = Exclude<(typeof KINDS)[number], TrajectoryRow["kind"]>;
    const noMissing: never[] = [] as MissingKind[];
    const noExtra: never[] = [] as ExtraKind[];
    expect(noMissing).toEqual([]);
    expect(noExtra).toEqual([]);

    expect(Object.keys(LANE_OF_KIND).sort()).toEqual([...KINDS].sort());
    const lanes: Lane[] = KINDS.map((k) => LANE_OF_KIND[k]);
    expect(new Set(lanes)).toEqual(new Set<Lane>(["user", "model", "tools"]));
  });
});
